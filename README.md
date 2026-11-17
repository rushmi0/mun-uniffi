# mun-uniffi

A template for integrating Rust with Kotlin Multiplatform (KMP) using [UniFFI](https://mozilla.github.io/uniffi-rs/) and [gobley](https://gobley.dev/).

The template provides a complete build pipeline for exposing a Rust crate to Kotlin Multiplatform targets, including JVM, Android, and iOS. The entire workflow is managed through a single `justfile`.

## Supported Platforms

* JVM
* Android
* iOS
* Linux
* macOS
* Windows

## Prerequisites

* [pixi](https://pixi.sh) (`curl -fsSL https://pixi.sh/install.sh | sh`): everything else except Rust (`pixi.toml`). That covers just, JDK 21 and LLVM 14 (`llvmdev`) on Linux, macOS and Windows, plus zig, clang-cl, cmake and ninja for the Linux cross builds.
* [rustup](https://rustup.rs): Rust is pinned to `1.89.0` by `rust-toolchain.toml`, which also lists every cross target.
* A host C compiler (`gcc` or `clang`) for cargo build scripts.

No containers, Android Studio or system packages are needed.

## Getting Started

```bash
pixi run -e cross setup   # Linux, once: LLVM 14 source, Android NDK r30 + SDK, cargo-xwin
pixi run just             # full pipeline for this host
```

Or open a shell with the environment and use `just` directly:

```bash
pixi shell
just android
```

On Linux x86_64 the full pipeline cross-builds Android, Linux (glibc + musl), FreeBSD and Windows, regenerates the bindings and assembles the KMP library. On macOS and Windows it builds that host's library with plain cargo.

`pixi run -e cross setup` downloads into `.toolchains/` (gitignored, kept by `pixi clean`), with checksums verified:

| What | Where | Source |
|------|-------|--------|
| LLVM 14.0.6 source (+ `<cstdint>` patch) | `.toolchains/src` | GitHub release |
| Android NDK r30 | `.toolchains/android-ndk-r30` | [NDK downloads](https://developer.android.com/ndk/downloads) |
| Android SDK (platform + build-tools for Gradle) | `.toolchains/android-sdk` | command-line tools + `sdkmanager` |
| cargo-xwin | `.pixi/tools/bin` | `cargo install --locked` |

### LLVM 14 for every target

The Mun compiler links LLVM 14 and LLD statically, so every target needs its own LLVM 14. conda-forge's `llvmdev` only exists for host platforms (no Android, musl, armv7, i686, riscv64, FreeBSD or Windows arm64 builds). `scripts/cross.sh` therefore builds a missing target's LLVM from source on first use, about 20-30 minutes per target, with the same toolchain the library is linked with:

| Targets | Toolchain | Installed to |
|---------|-----------|--------------|
| Android (API 24) | NDK r30 clang | `.toolchains/llvm14/android/<abi>` |
| Linux glibc/musl, FreeBSD | zig (glibc 2.17, riscv64 2.27) | `.toolchains/llvm14/linux/<target>` |
| Windows MSVC | clang-cl + cargo-xwin | `.toolchains/llvm14/windows/<target>` |

To build them ahead of time: `just build-llvm <target>...`. Locations can be overridden through the environment (see the top of `scripts/cross.sh`), for example to reuse existing builds:

```bash
LLVM14_ROOT=/path/to/llvm14 NDK_DIR=/path/to/android-ndk-r30 pixi run just
```

## Common Commands

### Build Everything

Build all supported targets, generate Kotlin bindings, and assemble the KMP library.

```bash
just
```

### Build for the Host

Build the Rust library for the host Linux x86_64 target.

```bash
just build-host
```

### Generate Kotlin Bindings

Regenerate the Kotlin bindings based on the current Rust API.

```bash
just generate-bindings
```

### Build the KMP Library

Assemble the Kotlin Multiplatform library using Gradle.

```bash
just build-kmp
```

### Run KMP Tests

Run JVM and Android host tests.

```bash
just test-kmp
```

Run JVM tests only:

```bash
just test-kmp-jvm
```

### Publish to Local Maven Repository

Publish the KMP library to the local Maven repository.

```bash
just publish-local
```

The library will be available under:

```text
~/.m2
```

### Clean Build Outputs

Remove generated build artifacts and outputs.

```bash
just clean
```

## Mun Script Engine

The crate embeds the [Mun](https://mun-lang.org/) compiler and runtime and exposes them to Kotlin as `MunEngine`, a small `eval()`-style script engine (see `src/engine.rs`).

```kotlin
MunEngine().use { engine ->
    // Compiles and loads the source. Runs `pub fn main()` if it has no parameters.
    engine.eval("pub fn main() -> i32 { 40 + 2 }")           // MunValue.I32(42)

    engine.eval("pub fn op(a: i32, b: i32) -> i32 { a + b }") // null: no main
    engine.call("op", listOf(MunValue.I32(6), MunValue.I32(7))) // MunValue.I32(13)

    // Hot reload: the same runtime picks up the recompiled library in place.
    engine.eval("pub fn op(a: i32, b: i32) -> i32 { a * b }")
    engine.call("op", listOf(MunValue.I32(6), MunValue.I32(7))) // MunValue.I32(42)
}
```

* `eval(source)` replaces the engine's single module. Identical source is a no-op.
* If the new source fails to compile, it throws `MunException.Compile` with the compiler diagnostics, and the previously loaded code keeps running.
* `call(name, args)` calls any `pub fn`. Integer arguments convert between `I32`/`I64` (range checked) and float arguments between `F32`/`F64`.

Each engine compiles into its own temporary directory. On Android before 13, `TMPDIR` is unset and the default `/data/local/tmp` is not writable by apps, so the engine falls back to the app's cache directory (`/data/user/<user>/<package>/cache`). Call `setTempDir(context.cacheDir.absolutePath)` before the first `MunEngine()` to choose the directory explicitly. Reloading works through Mun's normal mechanism: the compiler rewrites the `.munlib` under its lockfile, and `eval` waits (up to 10 s) until `Runtime::update()` has relinked it.

`MunEngineTest.kt` in `libkmp/mun-kmp/src/commonTest` is a runnable example.

### Building

The crate depends on the Mun crates by path (`../crates/*`), so it has to sit inside a Mun checkout. `scripts/cross.sh` sets `LLVM_SYS_140_PREFIX` per target. To build the bindings and the Linux x86_64 JVM library, then test:

```bash
pixi run just host
pixi run just test-kmp
```

### Limitations

* **No iOS/macOS.** There is no LLVM 14 build for Apple targets yet, so the pipeline covers Android, Linux, FreeBSD and Windows.
* **Library size.** `libmun.so` is about 95 MB because LLVM is embedded.
* **Call boundary.** `call` and `main` only pass `bool`, `i32`, `i64`, `f32`, `f64` and `()`, with at most 3 arguments. Mun code can use structs and other types internally, but cannot pass them to or from Kotlin.
* **No top-level statements.** Mun only has items, so "execute" means running `pub fn main()`. Only `pub` functions can be called.
* **Panics abort.** The release profile uses `panic = "abort"`, so a panic inside the Mun runtime ends the host process instead of throwing.
* `cc` is pinned to 1.2.2 (same as Mun's lockfile). Newer versions put `llvm-config`'s `-std=c++14` after `-std=c++17`, which breaks `lld_rs`.

## Available Recipes

To view all available `just` recipes:

```bash
just --list
```

The `justfile` is the central entry point for the build, binding generation, testing, packaging, and publishing workflow.
