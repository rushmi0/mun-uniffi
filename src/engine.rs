//! A small script-engine style wrapper around the Mun compiler and runtime.
//!
//! Source code is compiled in-process to a `.munlib` in a private temporary
//! directory and loaded by a [`Runtime`]. Evaluating new source recompiles that
//! same library, which the runtime picks up through its regular hot-reload
//! mechanism, so state that lives in the runtime survives the update.

use std::cell::RefCell;
use std::ffi::c_void;
use std::ops::Range;
use std::sync::{Arc, Mutex};
use std::thread::sleep;
use std::time::{Duration, Instant};

use mun_compiler::{Config, DisplayColor, Driver, PathOrInline, RelativePathBuf};
use mun_runtime::{
    FunctionDefinition, FunctionPrototype, FunctionSignature, HasStaticType,
    IntoFunctionDefinition, Runtime, Type,
};
use tempfile::TempDir;

/// Name of the single module that holds the evaluated source.
const MODULE_PATH: &str = "mod.mun";

/// Function that [`MunEngine::eval`] runs after loading the source, if present.
const ENTRY_POINT: &str = "main";

/// How long to wait for the runtime to pick up a recompiled library.
const RELOAD_TIMEOUT: Duration = Duration::from_secs(10);

/// A value passed to or returned from a Mun function.
#[derive(Debug, Clone, PartialEq, uniffi::Enum)]
pub enum MunValue {
    Unit,
    Bool {
        value: bool,
    },
    I32 {
        value: i32,
    },
    I64 {
        value: i64,
    },
    F32 {
        value: f32,
    },
    F64 {
        value: f64,
    },
    /// A Mun string; only passed to host functions.
    Str {
        value: String,
    },
}

#[derive(Debug, thiserror::Error, uniffi::Error)]
// Flat, so Kotlin sees the diagnostic text as `Throwable.message`.
#[uniffi(flat_error)]
pub enum MunError {
    /// The source did not compile. The previously loaded code stays active.
    #[error("{message}")]
    Compile { message: String },
    /// The compiled library could not be loaded or reloaded.
    #[error("{message}")]
    Runtime { message: String },
    /// A function could not be called with the given arguments.
    #[error("{message}")]
    Invoke { message: String },
    /// A host function failed while Mun code was calling it.
    #[error("{message}")]
    Host { message: String },
}

impl MunError {
    fn compile(message: impl ToString) -> Self {
        Self::Compile {
            message: message.to_string(),
        }
    }

    fn runtime(message: impl ToString) -> Self {
        Self::Runtime {
            message: message.to_string(),
        }
    }

    fn invoke(message: impl ToString) -> Self {
        Self::Invoke {
            message: message.to_string(),
        }
    }
}

/// The type of a host function parameter or return value.
#[derive(Debug, Clone, Copy, PartialEq, Eq, uniffi::Enum)]
pub enum MunType {
    /// No value; only valid as a return type.
    Unit,
    Bool,
    I32,
    I64,
    F32,
    F64,
    /// A string, which Mun represents as a `[u8]` array of UTF-8 bytes; only
    /// valid as a parameter type.
    Str,
}

impl MunType {
    fn type_info(self) -> Type {
        match self {
            Self::Unit => <()>::type_info().clone(),
            Self::Bool => bool::type_info().clone(),
            Self::I32 => i32::type_info().clone(),
            Self::I64 => i64::type_info().clone(),
            Self::F32 => f32::type_info().clone(),
            Self::F64 => f64::type_info().clone(),
            Self::Str => u8::type_info().array_type(),
        }
    }

    /// The name of the type in Mun source code.
    fn mun_name(self) -> &'static str {
        match self {
            Self::Unit => "()",
            Self::Bool => "bool",
            Self::I32 => "i32",
            Self::I64 => "i64",
            Self::F32 => "f32",
            Self::F64 => "f64",
            Self::Str => "[u8]",
        }
    }

    /// The suffix of the host entry points for this type, e.g. `__host_arg_i32`.
    fn suffix(self) -> &'static str {
        match self {
            Self::Unit => "unit",
            Self::Bool => "bool",
            Self::I32 => "i32",
            Self::I64 => "i64",
            Self::F32 => "f32",
            Self::F64 => "f64",
            Self::Str => "str",
        }
    }
}

/// A function implemented by the host that Mun code calls through an
/// `extern fn` declaration. Register it with [`MunEngine::register_fn`].
#[uniffi::export(with_foreign)]
pub trait HostFunction: Send + Sync {
    /// Called with arguments matching the registered parameter types; must
    /// return a value of the registered return type.
    fn call(&self, args: Vec<MunValue>) -> Result<MunValue, HostError>;
}

/// An error raised by a [`HostFunction`].
#[derive(Debug, thiserror::Error, uniffi::Error)]
pub enum HostError {
    #[error("{reason}")]
    Failed { reason: String },
}

impl From<uniffi::UnexpectedUniFFICallbackError> for HostError {
    fn from(err: uniffi::UnexpectedUniFFICallbackError) -> Self {
        Self::Failed { reason: err.reason }
    }
}

/// Sets the directory for all temporary files: the engines' output
/// directories, and the object files, libraries and library copies the Mun
/// compiler and runtime create while loading code.
///
/// Optional on Android: before Android 13 the default is `/data/local/tmp`,
/// which apps cannot write to, so [`MunEngine::new`] falls back to the app's
/// cache directory. Call it with `context.cacheDir.absolutePath` before
/// creating the first [`MunEngine`] to choose the directory explicitly. The
/// directory can only be set once per process; setting the same directory
/// again succeeds.
#[uniffi::export]
pub fn set_temp_dir(path: String) -> Result<(), MunError> {
    let path = std::path::PathBuf::from(path);
    std::fs::create_dir_all(&path)
        .map_err(|err| MunError::runtime(format!("{}: {err}", path.display())))?;
    match tempfile::env::override_temp_dir(&path) {
        Ok(()) => Ok(()),
        Err(current) if current == path => Ok(()),
        Err(current) => Err(MunError::runtime(format!(
            "the temporary directory is already set to {}",
            current.display()
        ))),
    }
}

/// Points the temporary directory at the app's cache directory when neither
/// [`set_temp_dir`] nor `TMPDIR` configured one.
///
/// Android only sets `TMPDIR` for apps since Android 13; before that
/// [`std::env::temp_dir`] falls back to `/data/local/tmp`, which apps cannot
/// write to. The cache directory is derived from the process name, which is
/// the package name, optionally followed by `:<process>`.
#[cfg(target_os = "android")]
fn ensure_android_temp_dir() {
    if std::env::var_os("TMPDIR").is_some() {
        return;
    }
    let Some(cache_dir) = android_cache_dir() else {
        return;
    };
    if std::fs::create_dir_all(&cache_dir).is_ok() {
        // Fails only if `set_temp_dir` already chose a directory, which wins.
        let _ = tempfile::env::override_temp_dir(&cache_dir);
    }
}

#[cfg(target_os = "android")]
fn android_cache_dir() -> Option<std::path::PathBuf> {
    let cmdline = std::fs::read("/proc/self/cmdline").ok()?;
    let process = cmdline.split(|&b| b == 0).next()?;
    let process = std::str::from_utf8(process).ok()?;
    let package = process.split(':').next()?;
    if package.is_empty() || package.contains('/') {
        return None;
    }

    // Each Android user owns a range of 100000 uids.
    let status = std::fs::read_to_string("/proc/self/status").ok()?;
    let uid: u32 = status
        .lines()
        .find_map(|line| line.strip_prefix("Uid:"))?
        .split_whitespace()
        .next()?
        .parse()
        .ok()?;
    let user = uid / 100_000;

    Some(std::path::PathBuf::from(format!(
        "/data/user/{user}/{package}/cache"
    )))
}

/// Compiles and runs Mun source code, hot reloading it on every change.
#[derive(uniffi::Object)]
pub struct MunEngine {
    // Declared before `out_dir` so the runtime unloads the library before the
    // directory holding it is removed.
    session: Mutex<Option<Session>>,
    /// Indexed by the id the generated Mun wrappers pass to `__host_call_*`.
    host_fns: Mutex<Arc<Vec<HostFn>>>,
    out_dir: TempDir,
}

struct Session {
    driver: Driver,
    runtime: Runtime,
    source: String,
}

#[uniffi::export]
impl MunEngine {
    #[uniffi::constructor]
    pub fn new() -> Result<Arc<Self>, MunError> {
        #[cfg(target_os = "android")]
        ensure_android_temp_dir();

        let out_dir = TempDir::new().map_err(|err| {
            if cfg!(target_os = "android") {
                MunError::runtime(format!(
                    "{err}; call setTempDir(context.cacheDir.absolutePath) before creating a MunEngine"
                ))
            } else {
                MunError::runtime(err)
            }
        })?;
        Ok(Arc::new(Self {
            session: Mutex::new(None),
            host_fns: Mutex::default(),
            out_dir,
        }))
    }

    /// Provides the implementation of the Mun `extern fn name(..)` declaration.
    ///
    /// Must be called before the first [`eval`](Self::eval). `func` runs on the
    /// thread that called into Mun, while this engine is locked, so it must not
    /// call back into the engine. If it fails, Mun receives a zero value and
    /// the call into Mun returns [`MunError::Host`] once Mun code finishes.
    pub fn register_fn(
        &self,
        name: String,
        arg_types: Vec<MunType>,
        return_type: MunType,
        func: Arc<dyn HostFunction>,
    ) -> Result<(), MunError> {
        let session = self.lock();
        if session.is_some() {
            return Err(MunError::invoke(
                "host functions must be registered before the first eval",
            ));
        }

        let mut host_fns = lock(&self.host_fns);
        if host_fns.iter().any(|host_fn| host_fn.name == name) {
            return Err(MunError::invoke(format!(
                "host function '{name}' is already registered"
            )));
        }
        if arg_types.contains(&MunType::Unit) {
            return Err(MunError::invoke(format!(
                "host function '{name}' cannot take a Unit parameter"
            )));
        }
        if return_type == MunType::Str {
            return Err(MunError::invoke(format!(
                "host function '{name}' cannot return a Str"
            )));
        }

        Arc::make_mut(&mut host_fns).push(HostFn {
            name,
            arg_types,
            return_type,
            func,
        });
        Ok(())
    }

    /// Loads `source`, replacing any previously evaluated source in place, and
    /// runs its `pub fn main()` if it defines one without parameters.
    ///
    /// Returns the result of `main`, or `null` if there is no such function.
    pub fn eval(&self, source: String) -> Result<Option<MunValue>, MunError> {
        let mut session = self.lock();
        let source = with_host_fns(source, &lock(&self.host_fns));
        if let Some(current) = session.as_mut() {
            current.reload(source)?;
        } else {
            *session = Some(Session::load(&self.out_dir, source)?);
        }

        let current = session.as_ref().expect("session was just loaded");
        let has_entry_point = current
            .runtime
            .get_function_definition(ENTRY_POINT)
            .is_some_and(|def| def.prototype.signature.arg_types.is_empty());
        if has_entry_point {
            self.call_session(current, ENTRY_POINT, &[]).map(Some)
        } else {
            Ok(None)
        }
    }

    /// Calls the public Mun function `name` of the currently loaded source.
    pub fn call(&self, name: String, args: Vec<MunValue>) -> Result<MunValue, MunError> {
        let session = self.lock();
        let session = session
            .as_ref()
            .ok_or_else(|| MunError::invoke("no source has been evaluated yet"))?;
        self.call_session(session, &name, &args)
    }
}

impl MunEngine {
    fn lock(&self) -> std::sync::MutexGuard<'_, Option<Session>> {
        lock(&self.session)
    }

    /// Calls into Mun, reporting the first host function failure, if any,
    /// instead of the result Mun computed from its zero value.
    fn call_session(
        &self,
        session: &Session,
        name: &str,
        args: &[MunValue],
    ) -> Result<MunValue, MunError> {
        let host_fns = Arc::clone(&lock(&self.host_fns));
        let (result, host_error) = HostCall::run(host_fns, || session.call(name, args));
        match host_error {
            Some(message) => Err(MunError::Host { message }),
            None => result,
        }
    }
}

fn lock<T>(mutex: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    // A panic while holding any of the engine's locks leaves its data in a
    // valid state, so there is no need to propagate the poison.
    mutex
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

/// A registered [`HostFunction`] and its signature.
#[derive(Clone)]
struct HostFn {
    name: String,
    arg_types: Vec<MunType>,
    return_type: MunType,
    func: Arc<dyn HostFunction>,
}

// Mun calls host functions through a fixed set of native entry points, the way
// QuickJS calls every Rust closure through one C function in rquickjs: each
// registered function gets a generated Mun wrapper (see `with_host_fns`) that
// pushes its arguments with `__host_arg_<type>` and then calls
// `__host_call_<return type>(id)`. The id selects the `HostFn`, so no native
// code has to be generated at runtime for each signature.

/// The host functions and pending arguments of the call into Mun running on
/// this thread.
struct HostCall {
    host_fns: Arc<Vec<HostFn>>,
    args: Vec<MunValue>,
    /// The first host function failure, reported once Mun code returns.
    error: Option<String>,
}

thread_local! {
    static HOST_CALL: RefCell<Option<HostCall>> = const { RefCell::new(None) };
}

impl HostCall {
    /// Runs `f`, a call into Mun, with `host_fns` available to the entry points.
    fn run<T>(host_fns: Arc<Vec<HostFn>>, f: impl FnOnce() -> T) -> (T, Option<String>) {
        let call = HostCall {
            host_fns,
            args: Vec::new(),
            error: None,
        };
        let outer = HOST_CALL.replace(Some(call));
        let result = f();
        let call = HOST_CALL.replace(outer);
        (result, call.and_then(|call| call.error))
    }

    fn push_arg(value: MunValue) {
        HOST_CALL.with_borrow_mut(|call| {
            if let Some(call) = call {
                call.args.push(value);
            }
        });
    }

    /// Takes the pushed arguments for a call to host function `id`.
    fn take(id: i64) -> Option<(HostFn, Vec<MunValue>)> {
        HOST_CALL.with_borrow_mut(|call| {
            let call = call.as_mut()?;
            let host_fn = usize::try_from(id)
                .ok()
                .and_then(|id| call.host_fns.get(id))?;
            Some((host_fn.clone(), std::mem::take(&mut call.args)))
        })
    }

    fn fail(message: String) {
        HOST_CALL.with_borrow_mut(|call| {
            if let Some(call) = call {
                call.error.get_or_insert(message);
            }
        });
    }
}

/// Calls host function `id` with the pushed arguments and converts its return
/// value to `T`. On failure, records the error and returns zero.
fn call_host<T: FromMunValue + HasStaticType + Default>(id: i64) -> T {
    let Some((host_fn, args)) = HostCall::take(id) else {
        return T::default();
    };
    // The callback runs without `HOST_CALL` borrowed, so it may call into
    // another engine.
    let result = match host_fn.func.call(args) {
        Ok(value) => convert::<T>(value).map_err(|err| {
            format!(
                "host function '{}' returned an invalid value: {err}",
                host_fn.name
            )
        }),
        Err(HostError::Failed { reason }) => {
            Err(format!("host function '{}' failed: {reason}", host_fn.name))
        }
    };
    result.unwrap_or_else(|message| {
        HostCall::fail(message);
        T::default()
    })
}

// Mun passes `bool` as an LLVM `i1`, which the C ABI lowers to a byte.
extern "C" fn host_arg_bool(value: u8) {
    HostCall::push_arg(MunValue::Bool { value: value != 0 });
}

extern "C" fn host_arg_i32(value: i32) {
    HostCall::push_arg(MunValue::I32 { value });
}

extern "C" fn host_arg_i64(value: i64) {
    HostCall::push_arg(MunValue::I64 { value });
}

extern "C" fn host_arg_f32(value: f32) {
    HostCall::push_arg(MunValue::F32 { value });
}

extern "C" fn host_arg_f64(value: f64) {
    HostCall::push_arg(MunValue::F64 { value });
}

extern "C" fn host_arg_str(handle: *const *const ArrayHeader) {
    // SAFETY: Mun passes the GC handle of a live `[u8]` array.
    let value = unsafe { read_str(handle) };
    HostCall::push_arg(MunValue::Str { value });
}

extern "C" fn host_call_unit(id: i64) {
    call_host::<()>(id);
}

extern "C" fn host_call_bool(id: i64) -> bool {
    call_host(id)
}

extern "C" fn host_call_i32(id: i64) -> i32 {
    call_host(id)
}

extern "C" fn host_call_i64(id: i64) -> i64 {
    call_host(id)
}

extern "C" fn host_call_f32(id: i64) -> f32 {
    call_host(id)
}

extern "C" fn host_call_f64(id: i64) -> f64 {
    call_host(id)
}

/// A native function Mun code can call through an `extern fn` declaration.
struct EntryPoint {
    name: String,
    arg_types: Vec<MunType>,
    return_type: MunType,
    fn_ptr: *const c_void,
}

impl EntryPoint {
    fn new(
        name: &str,
        ty: MunType,
        arg_types: &[MunType],
        return_type: MunType,
        fn_ptr: *const c_void,
    ) -> Self {
        Self {
            name: format!("__host_{name}_{}", ty.suffix()),
            arg_types: arg_types.to_vec(),
            return_type,
            fn_ptr,
        }
    }

    /// The Mun `extern fn` declaration of this entry point.
    fn declaration(&self) -> String {
        let params = self
            .arg_types
            .iter()
            .enumerate()
            .map(|(i, ty)| format!("arg{i}: {}", ty.mun_name()))
            .collect::<Vec<_>>()
            .join(", ");
        format!(
            "extern fn {}({params}){};",
            self.name,
            return_suffix(self.return_type)
        )
    }
}

impl IntoFunctionDefinition for &EntryPoint {
    fn into<S: Into<String>>(self, name: S) -> FunctionDefinition {
        FunctionDefinition {
            prototype: FunctionPrototype {
                name: name.into(),
                signature: FunctionSignature {
                    arg_types: self.arg_types.iter().map(|ty| ty.type_info()).collect(),
                    return_type: self.return_type.type_info(),
                },
            },
            fn_ptr: self.fn_ptr,
        }
    }
}

fn entry_points() -> Vec<EntryPoint> {
    use MunType::{Bool, F32, F64, I32, I64, Str, Unit};
    let arg = |ty, fn_ptr| EntryPoint::new("arg", ty, &[ty], Unit, fn_ptr);
    let call = |ty, fn_ptr| EntryPoint::new("call", ty, &[I64], ty, fn_ptr);
    vec![
        arg(Bool, host_arg_bool as *const c_void),
        arg(I32, host_arg_i32 as *const c_void),
        arg(I64, host_arg_i64 as *const c_void),
        arg(F32, host_arg_f32 as *const c_void),
        arg(F64, host_arg_f64 as *const c_void),
        arg(Str, host_arg_str as *const c_void),
        call(Unit, host_call_unit as *const c_void),
        call(Bool, host_call_bool as *const c_void),
        call(I32, host_call_i32 as *const c_void),
        call(I64, host_call_i64 as *const c_void),
        call(F32, host_call_f32 as *const c_void),
        call(F64, host_call_f64 as *const c_void),
    ]
}

/// The header of a Mun array, followed by its elements.
#[repr(C)]
struct ArrayHeader {
    length: usize,
    _capacity: usize,
}

/// Reads a Mun string, a `[u8]` array of UTF-8 bytes.
///
/// # Safety
///
/// `handle` must be the GC handle of a live `[u8]` array.
unsafe fn read_str(handle: *const *const ArrayHeader) -> String {
    // SAFETY: guaranteed by the caller; `u8` elements directly follow the
    // header, which is already aligned for them.
    let bytes = unsafe {
        let header = *handle;
        std::slice::from_raw_parts(header.add(1).cast::<u8>(), (*header).length)
    };
    String::from_utf8_lossy(bytes).into_owned()
}

/// Makes the host functions callable from `source`: appends the entry point
/// declarations and a wrapper for each host function the source does not
/// define itself. The source may still declare a host function as
/// `extern fn name(..);`, as plain Mun requires; that declaration is blanked
/// out in favor of the wrapper. Only appending and blanking keep the line
/// numbers in diagnostics intact.
fn with_host_fns(mut source: String, host_fns: &[HostFn]) -> String {
    if host_fns.is_empty() {
        return source;
    }
    for host_fn in host_fns {
        while let Some(range) = extern_declaration(&source, &host_fn.name) {
            let blank: String = source[range.clone()]
                .chars()
                .map(|c| if c == '\n' { '\n' } else { ' ' })
                .collect();
            source.replace_range(range, &blank);
        }
    }

    for entry_point in entry_points() {
        source.push('\n');
        source.push_str(&entry_point.declaration());
    }
    for (id, host_fn) in host_fns.iter().enumerate() {
        if !defines_fn(&source, &host_fn.name) {
            source.push('\n');
            source.push_str(&wrapper(id, host_fn));
        }
    }
    source
}

/// The Mun function that forwards a call to host function `id`.
fn wrapper(id: usize, host_fn: &HostFn) -> String {
    let params = host_fn
        .arg_types
        .iter()
        .enumerate()
        .map(|(i, ty)| format!("arg{i}: {}", ty.mun_name()))
        .collect::<Vec<_>>()
        .join(", ");
    let push_args: String = host_fn
        .arg_types
        .iter()
        .enumerate()
        .map(|(i, ty)| format!("__host_arg_{}(arg{i}); ", ty.suffix()))
        .collect();
    format!(
        "fn {}({params}){} {{ {push_args}__host_call_{}({id}) }}",
        host_fn.name,
        return_suffix(host_fn.return_type),
        host_fn.return_type.suffix()
    )
}

fn return_suffix(ty: MunType) -> String {
    match ty {
        MunType::Unit => String::new(),
        ty => format!(" -> {}", ty.mun_name()),
    }
}

/// Byte ranges of `fn name(` in `source`, each starting at its `fn` keyword.
fn fn_items<'a>(source: &'a str, name: &'a str) -> impl Iterator<Item = Range<usize>> + 'a {
    let is_ident = |c: char| c.is_alphanumeric() || c == '_';
    source.match_indices(name).filter_map(move |(start, _)| {
        let before = &source[..start];
        let keyword = before.trim_end().strip_suffix("fn")?;
        let separated = before.ends_with(char::is_whitespace) && !keyword.ends_with(is_ident);
        let is_call = source[start + name.len()..].trim_start().starts_with('(');
        (separated && is_call).then(|| keyword.len()..start + name.len())
    })
}

/// The range of an `extern fn name(..);` declaration in `source`, if any.
fn extern_declaration(source: &str, name: &str) -> Option<Range<usize>> {
    fn_items(source, name).find_map(|item| {
        let before = source[..item.start].trim_end();
        let extern_start = before.strip_suffix("extern")?.len();
        let end = item.end + source[item.end..].find(';')? + 1;
        Some(extern_start..end)
    })
}

/// Returns whether `source` defines (or declares) a function called `name`.
fn defines_fn(source: &str, name: &str) -> bool {
    fn_items(source, name).next().is_some()
}

/// Calls `runtime.invoke` with argument and return types matching a signature
/// that is only known at runtime. Each `(value, declared type)` pair is
/// converted in turn, after which the return type is resolved.
macro_rules! invoke_typed {
    (@call $runtime:expr, $name:expr, $ret:ty, [$($done:expr),*]) => {
        $runtime
            .invoke::<$ret, _>($name, ($($done,)*))
            .map_err(|err| MunError::invoke(err))
    };
    ($runtime:expr, $name:expr, $ret:expr, [], [$($done:expr),*]) => {{
        let ret: &Type = $ret;
        if ret == <()>::type_info() {
            invoke_typed!(@call $runtime, $name, (), [$($done),*]).map(|()| MunValue::Unit)
        } else if ret == bool::type_info() {
            invoke_typed!(@call $runtime, $name, bool, [$($done),*]).map(|value| MunValue::Bool { value })
        } else if ret == i32::type_info() {
            invoke_typed!(@call $runtime, $name, i32, [$($done),*]).map(|value| MunValue::I32 { value })
        } else if ret == i64::type_info() {
            invoke_typed!(@call $runtime, $name, i64, [$($done),*]).map(|value| MunValue::I64 { value })
        } else if ret == f32::type_info() {
            invoke_typed!(@call $runtime, $name, f32, [$($done),*]).map(|value| MunValue::F32 { value })
        } else if ret == f64::type_info() {
            invoke_typed!(@call $runtime, $name, f64, [$($done),*]).map(|value| MunValue::F64 { value })
        } else {
            Err(unsupported(ret))
        }
    }};
    ($runtime:expr, $name:expr, $ret:expr, [($value:expr, $ty:expr) $(, $rest:tt)*], [$($done:expr),*]) => {{
        let value: MunValue = $value;
        let ty: &Type = $ty;
        if ty == bool::type_info() {
            let arg = convert::<bool>(value)?;
            invoke_typed!($runtime, $name, $ret, [$($rest),*], [$($done,)* arg])
        } else if ty == i32::type_info() {
            let arg = convert::<i32>(value)?;
            invoke_typed!($runtime, $name, $ret, [$($rest),*], [$($done,)* arg])
        } else if ty == i64::type_info() {
            let arg = convert::<i64>(value)?;
            invoke_typed!($runtime, $name, $ret, [$($rest),*], [$($done,)* arg])
        } else if ty == f32::type_info() {
            let arg = convert::<f32>(value)?;
            invoke_typed!($runtime, $name, $ret, [$($rest),*], [$($done,)* arg])
        } else if ty == f64::type_info() {
            let arg = convert::<f64>(value)?;
            invoke_typed!($runtime, $name, $ret, [$($rest),*], [$($done,)* arg])
        } else {
            Err(unsupported(ty))
        }
    }};
}

// SAFETY: `Session` is only ever accessed through the `Mutex` in `MunEngine`,
// so the compiler database and runtime are never used from two threads at the
// same time. Neither keeps thread-local state between calls.
unsafe impl Send for Session {}

impl Session {
    fn load(out_dir: &TempDir, source: String) -> Result<Self, MunError> {
        let config = Config {
            out_dir: Some(out_dir.path().to_path_buf()),
            ..Config::default()
        };
        let input = PathOrInline::Inline {
            rel_path: RelativePathBuf::from(MODULE_PATH),
            contents: source.clone(),
        };
        let (mut driver, file_id) = Driver::with_file(config, input).map_err(MunError::compile)?;
        check_diagnostics(&driver)?;
        driver
            .write_all_assemblies(true)
            .map_err(MunError::compile)?;

        let lib_path = driver.assembly_output_path_from_file(file_id);
        let builder = entry_points()
            .iter()
            .fold(Runtime::builder(lib_path), |builder, entry_point| {
                builder.insert_fn(entry_point.name.clone(), entry_point)
            });
        // SAFETY: the library was just produced by the Mun compiler from
        // `source`, and the entry points are plain functions of this library.
        let runtime = unsafe { builder.finish() }.map_err(MunError::runtime)?;

        Ok(Self {
            driver,
            runtime,
            source,
        })
    }

    fn reload(&mut self, source: String) -> Result<(), MunError> {
        if source == self.source {
            return Ok(());
        }

        self.set_source(&source)?;
        if let Err(err) = check_diagnostics(&self.driver) {
            // Keep the compiler in sync with the code that is still loaded.
            let previous = std::mem::take(&mut self.source);
            self.set_source(&previous)?;
            self.source = previous;
            return Err(err);
        }

        // Always write the library: the runtime only reloads once it observes a
        // modified `.munlib`, and an unchanged one would make us wait forever.
        self.driver
            .write_all_assemblies(true)
            .map_err(MunError::compile)?;
        self.source = source;

        let start = Instant::now();
        // SAFETY: the library was just produced by the Mun compiler from `source`.
        while !unsafe { self.runtime.update() } {
            if start.elapsed() > RELOAD_TIMEOUT {
                return Err(MunError::runtime(
                    "the runtime did not reload the recompiled library in time",
                ));
            }
            sleep(Duration::from_millis(1));
        }
        Ok(())
    }

    fn set_source(&mut self, source: &str) -> Result<(), MunError> {
        self.driver
            .set_file_text(MODULE_PATH, source)
            .map_err(MunError::compile)
    }

    fn call(&self, name: &str, args: &[MunValue]) -> Result<MunValue, MunError> {
        let definition = self
            .runtime
            .get_function_definition(name)
            .ok_or_else(|| MunError::invoke(format!("no public function named '{name}'")))?;
        let signature = &definition.prototype.signature;
        if signature.arg_types.len() != args.len() {
            return Err(MunError::invoke(format!(
                "'{name}' expects {} arguments, got {}",
                signature.arg_types.len(),
                args.len()
            )));
        }

        let runtime = &self.runtime;
        let ret = &signature.return_type;
        let types = &signature.arg_types;
        match args {
            [] => invoke_typed!(runtime, name, ret, [], []),
            [a] => invoke_typed!(runtime, name, ret, [(a.clone(), &types[0])], []),
            [a, b] => invoke_typed!(
                runtime,
                name,
                ret,
                [(a.clone(), &types[0]), (b.clone(), &types[1])],
                []
            ),
            [a, b, c] => invoke_typed!(
                runtime,
                name,
                ret,
                [
                    (a.clone(), &types[0]),
                    (b.clone(), &types[1]),
                    (c.clone(), &types[2])
                ],
                []
            ),
            _ => Err(MunError::invoke(format!(
                "calling functions with more than {MAX_ARGS} arguments is not supported"
            ))),
        }
    }
}

/// The largest number of arguments [`MunEngine::call`] can pass.
const MAX_ARGS: usize = 3;

fn check_diagnostics(driver: &Driver) -> Result<(), MunError> {
    match driver
        .emit_diagnostics_to_string(DisplayColor::Disable)
        .map_err(MunError::compile)?
    {
        Some(diagnostics) => Err(MunError::compile(diagnostics)),
        None => Ok(()),
    }
}

/// Converts a [`MunValue`] to the parameter type a Mun function declares.
trait FromMunValue: Sized {
    fn from_mun_value(value: &MunValue) -> Option<Self>;
}

impl FromMunValue for () {
    fn from_mun_value(value: &MunValue) -> Option<Self> {
        (*value == MunValue::Unit).then_some(())
    }
}

impl FromMunValue for bool {
    fn from_mun_value(value: &MunValue) -> Option<Self> {
        match *value {
            MunValue::Bool { value } => Some(value),
            _ => None,
        }
    }
}

macro_rules! impl_from_mun_value {
    ($($ty:ty),*) => {$(
        impl FromMunValue for $ty {
            fn from_mun_value(value: &MunValue) -> Option<Self> {
                match *value {
                    MunValue::I32 { value } => i64::from(value).try_into().ok(),
                    MunValue::I64 { value } => value.try_into().ok(),
                    _ => None,
                }
            }
        }
    )*};
}

impl_from_mun_value!(i32, i64);

impl FromMunValue for f32 {
    fn from_mun_value(value: &MunValue) -> Option<Self> {
        match *value {
            MunValue::F32 { value } => Some(value),
            #[allow(clippy::cast_possible_truncation)]
            MunValue::F64 { value } => Some(value as f32),
            _ => None,
        }
    }
}

impl FromMunValue for f64 {
    fn from_mun_value(value: &MunValue) -> Option<Self> {
        match *value {
            MunValue::F32 { value } => Some(f64::from(value)),
            MunValue::F64 { value } => Some(value),
            _ => None,
        }
    }
}

fn convert<T: FromMunValue + HasStaticType>(value: MunValue) -> Result<T, MunError> {
    T::from_mun_value(&value).ok_or_else(|| {
        MunError::invoke(format!(
            "cannot pass {value:?} as '{}'",
            T::type_info().name()
        ))
    })
}

fn unsupported(ty: &Type) -> MunError {
    MunError::invoke(format!(
        "type '{}' is not supported across the Kotlin boundary",
        ty.name()
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn eval_runs_main() {
        let engine = MunEngine::new().unwrap();
        let result = engine
            .eval("pub fn main() -> i32 { 40 + 2 }".into())
            .unwrap();
        assert_eq!(result, Some(MunValue::I32 { value: 42 }));
    }

    #[test]
    fn eval_without_main_returns_none() {
        let engine = MunEngine::new().unwrap();
        let result = engine.eval("pub fn add(a: i64, b: i64) -> i64 { a + b }".into());
        assert_eq!(result.unwrap(), None);
    }

    #[test]
    fn call_converts_arguments() {
        let engine = MunEngine::new().unwrap();
        engine
            .eval(
                "pub fn pick(flag: bool, a: i64, b: i64) -> i64 { if flag { a } else { b } }
                 pub fn half(x: f32) -> f32 { x / 2.0 }"
                    .into(),
            )
            .unwrap();
        let args = vec![
            MunValue::Bool { value: false },
            MunValue::I32 { value: 1 },
            MunValue::I32 { value: 2 },
        ];
        assert_eq!(
            engine.call("pick".into(), args).unwrap(),
            MunValue::I64 { value: 2 }
        );
        assert_eq!(
            engine
                .call("half".into(), vec![MunValue::F64 { value: 5.0 }])
                .unwrap(),
            MunValue::F32 { value: 2.5 }
        );

        let err = engine
            .call("half".into(), vec![MunValue::Bool { value: true }])
            .unwrap_err();
        assert!(matches!(err, MunError::Invoke { .. }), "{err:?}");
    }

    #[test]
    fn eval_hot_reloads() {
        let engine = MunEngine::new().unwrap();
        engine
            .eval("pub fn op(a: i32, b: i32) -> i32 { a + b }".into())
            .unwrap();
        let args = vec![MunValue::I32 { value: 6 }, MunValue::I32 { value: 7 }];
        assert_eq!(
            engine.call("op".into(), args.clone()).unwrap(),
            MunValue::I32 { value: 13 }
        );

        engine
            .eval("pub fn op(a: i32, b: i32) -> i32 { a * b }".into())
            .unwrap();
        assert_eq!(
            engine.call("op".into(), args).unwrap(),
            MunValue::I32 { value: 42 }
        );
    }

    #[test]
    fn compile_error_keeps_previous_code() {
        let engine = MunEngine::new().unwrap();
        engine.eval("pub fn main() -> i32 { 1 }".into()).unwrap();

        let err = engine
            .eval("pub fn main() -> i32 { true }".into())
            .unwrap_err();
        assert!(matches!(err, MunError::Compile { .. }), "{err:?}");

        let result = engine.eval("pub fn main() -> i32 { 1 }".into()).unwrap();
        assert_eq!(result, Some(MunValue::I32 { value: 1 }));
    }

    // The only test that sets the process-wide temporary directory.
    #[test]
    fn set_temp_dir_moves_output() {
        let dir = std::env::temp_dir().join("mun-engine-set-temp-dir");
        set_temp_dir(dir.to_string_lossy().into_owned()).unwrap();
        set_temp_dir(dir.to_string_lossy().into_owned()).unwrap();
        let err = set_temp_dir("/elsewhere".into()).unwrap_err();
        assert!(matches!(err, MunError::Runtime { .. }), "{err:?}");

        let engine = MunEngine::new().unwrap();
        assert!(engine.out_dir.path().starts_with(&dir));
        let result = engine.eval("pub fn main() -> i32 { 7 }".into()).unwrap();
        assert_eq!(result, Some(MunValue::I32 { value: 7 }));
    }

    #[test]
    fn call_reports_unknown_function() {
        let engine = MunEngine::new().unwrap();
        engine.eval("pub fn main() {}".into()).unwrap();
        let err = engine.call("missing".into(), vec![]).unwrap_err();
        assert!(matches!(err, MunError::Invoke { .. }), "{err:?}");
    }

    /// A host function backed by a Rust closure.
    struct Host<F>(F);

    impl<F> HostFunction for Host<F>
    where
        F: Fn(Vec<MunValue>) -> Result<MunValue, HostError> + Send + Sync,
    {
        fn call(&self, args: Vec<MunValue>) -> Result<MunValue, HostError> {
            (self.0)(args)
        }
    }

    fn host(
        f: impl Fn(Vec<MunValue>) -> Result<MunValue, HostError> + Send + Sync + 'static,
    ) -> Arc<dyn HostFunction> {
        Arc::new(Host(f))
    }

    #[test]
    fn extern_fn_calls_host() {
        let engine = MunEngine::new().unwrap();
        engine
            .register_fn(
                "random".into(),
                vec![],
                MunType::I64,
                host(|_| Ok(MunValue::I64 { value: 4 })),
            )
            .unwrap();
        let result = engine
            .eval(
                "extern fn random() -> i64;
                 pub fn main() -> bool { random() % 2 == 0 }"
                    .into(),
            )
            .unwrap();
        assert_eq!(result, Some(MunValue::Bool { value: true }));
    }

    #[test]
    fn extern_fn_passes_every_type() {
        let engine = MunEngine::new().unwrap();
        engine
            .register_fn(
                "mix".into(),
                vec![
                    MunType::Bool,
                    MunType::I32,
                    MunType::I64,
                    MunType::F32,
                    MunType::F64,
                ],
                MunType::F64,
                host(|args| match args[..] {
                    [
                        MunValue::Bool { value: flag },
                        MunValue::I32 { value: a },
                        MunValue::I64 { value: b },
                        MunValue::F32 { value: c },
                        MunValue::F64 { value: d },
                    ] => {
                        let sum = f64::from(a) + b as f64 + f64::from(c) + d;
                        Ok(MunValue::F64 {
                            value: if flag { sum } else { -sum },
                        })
                    }
                    _ => Err(HostError::Failed {
                        reason: format!("unexpected arguments {args:?}"),
                    }),
                }),
            )
            .unwrap();
        engine
            .eval(
                "extern fn mix(flag: bool, a: i32, b: i64, c: f32, d: f64) -> f64;
                 pub fn run(flag: bool) -> f64 { mix(flag, 1, 2, 0.5, 0.25) }"
                    .into(),
            )
            .unwrap();
        assert_eq!(
            engine
                .call("run".into(), vec![MunValue::Bool { value: true }])
                .unwrap(),
            MunValue::F64 { value: 3.75 }
        );
        assert_eq!(
            engine
                .call("run".into(), vec![MunValue::Bool { value: false }])
                .unwrap(),
            MunValue::F64 { value: -3.75 }
        );
    }

    #[test]
    fn extern_fn_without_return_value() {
        let seen = Arc::new(Mutex::new(Vec::new()));
        let engine = MunEngine::new().unwrap();
        let log = Arc::clone(&seen);
        engine
            .register_fn(
                "log".into(),
                vec![MunType::I32],
                MunType::Unit,
                host(move |args| {
                    log.lock().unwrap().extend(args);
                    Ok(MunValue::Unit)
                }),
            )
            .unwrap();
        engine
            .eval("extern fn log(v: i32); pub fn main() { log(1); log(2) }".into())
            .unwrap();
        assert_eq!(
            *seen.lock().unwrap(),
            [MunValue::I32 { value: 1 }, MunValue::I32 { value: 2 }]
        );
    }

    #[test]
    fn registered_println_needs_no_declaration() {
        let printed = Arc::new(Mutex::new(Vec::new()));
        let engine = MunEngine::new().unwrap();
        let out = Arc::clone(&printed);
        engine
            .register_fn(
                "println".into(),
                vec![MunType::I64],
                MunType::Unit,
                host(move |args| {
                    out.lock().unwrap().extend(args);
                    Ok(MunValue::Unit)
                }),
            )
            .unwrap();
        engine
            .eval("pub fn main() { println(7); println(-1) }".into())
            .unwrap();
        assert_eq!(
            *printed.lock().unwrap(),
            [MunValue::I64 { value: 7 }, MunValue::I64 { value: -1 }]
        );
    }

    #[test]
    fn extern_fn_survives_hot_reload() {
        let engine = MunEngine::new().unwrap();
        engine
            .register_fn(
                "base".into(),
                vec![],
                MunType::I32,
                host(|_| Ok(MunValue::I32 { value: 10 })),
            )
            .unwrap();
        let source =
            |op: &str| format!("extern fn base() -> i32; pub fn main() -> i32 {{ base() {op} 2 }}");
        assert_eq!(
            engine.eval(source("+")).unwrap(),
            Some(MunValue::I32 { value: 12 })
        );
        assert_eq!(
            engine.eval(source("*")).unwrap(),
            Some(MunValue::I32 { value: 20 })
        );
    }

    #[test]
    fn host_failure_is_reported() {
        let engine = MunEngine::new().unwrap();
        engine
            .register_fn(
                "fails".into(),
                vec![],
                MunType::I32,
                host(|_| {
                    Err(HostError::Failed {
                        reason: "boom".into(),
                    })
                }),
            )
            .unwrap();
        engine
            .register_fn(
                "wrong".into(),
                vec![],
                MunType::I32,
                host(|_| Ok(MunValue::Bool { value: true })),
            )
            .unwrap();
        engine
            .eval(
                "extern fn fails() -> i32; extern fn wrong() -> i32;
                 pub fn a() -> i32 { fails() }
                 pub fn b() -> i32 { wrong() }"
                    .into(),
            )
            .unwrap();

        let err = engine.call("a".into(), vec![]).unwrap_err();
        assert!(
            matches!(&err, MunError::Host { message } if message.contains("boom")),
            "{err:?}"
        );
        let err = engine.call("b".into(), vec![]).unwrap_err();
        assert!(matches!(err, MunError::Host { .. }), "{err:?}");
    }

    #[test]
    fn host_fn_named_like_a_libc_symbol() {
        // Calls to `log` must reach the host, not libm's `log`.
        let calls = Arc::new(Mutex::new(Vec::new()));
        let engine = MunEngine::new().unwrap();
        let seen = Arc::clone(&calls);
        engine
            .register_fn(
                "log".into(),
                vec![MunType::F64],
                MunType::Unit,
                host(move |args| {
                    seen.lock().unwrap().extend(args);
                    Ok(MunValue::Unit)
                }),
            )
            .unwrap();
        engine.eval("pub fn main() { log(2.5) }".into()).unwrap();
        assert_eq!(*calls.lock().unwrap(), [MunValue::F64 { value: 2.5 }]);
    }

    #[test]
    fn extern_fn_receives_strings() {
        let engine = MunEngine::new().unwrap();
        engine
            .register_fn(
                "len".into(),
                vec![MunType::Str, MunType::I32],
                MunType::I64,
                host(|args| match &args[..] {
                    [MunValue::Str { value }, MunValue::I32 { value: extra }] => {
                        Ok(MunValue::I64 {
                            value: value.len() as i64 + i64::from(*extra),
                        })
                    }
                    _ => Err(HostError::Failed {
                        reason: format!("unexpected arguments {args:?}"),
                    }),
                }),
            )
            .unwrap();
        let result = engine
            .eval(
                // Mun strings are `[u8]` arrays of UTF-8 bytes; this is "héllo".
                "pub fn main() -> i64 { len([104, 195, 169, 108, 108, 111], 1) }".into(),
            )
            .unwrap();
        assert_eq!(result, Some(MunValue::I64 { value: 7 }));
    }

    #[test]
    fn missing_host_fn_fails_to_load() {
        let engine = MunEngine::new().unwrap();
        let err = engine
            .eval("extern fn random() -> i64; pub fn main() -> i64 { random() }".into())
            .unwrap_err();
        assert!(matches!(err, MunError::Runtime { .. }), "{err:?}");
    }

    #[test]
    fn register_fn_validates() {
        let engine = MunEngine::new().unwrap();
        let unit = || host(|_| Ok(MunValue::Unit));
        let err = engine
            .register_fn("f".into(), vec![MunType::Unit], MunType::Unit, unit())
            .unwrap_err();
        assert!(matches!(err, MunError::Invoke { .. }), "{err:?}");

        engine
            .register_fn("f".into(), vec![], MunType::Unit, unit())
            .unwrap();
        let err = engine
            .register_fn("f".into(), vec![], MunType::Unit, unit())
            .unwrap_err();
        assert!(matches!(err, MunError::Invoke { .. }), "{err:?}");

        engine.eval("pub fn main() {}".into()).unwrap();
        let err = engine
            .register_fn("g".into(), vec![], MunType::Unit, unit())
            .unwrap_err();
        assert!(matches!(err, MunError::Invoke { .. }), "{err:?}");
    }
}
