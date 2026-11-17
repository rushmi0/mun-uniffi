set shell := ["bash", "-euo", "pipefail", "-c"]
set windows-shell := ["powershell.exe", "-NoLogo", "-NoProfile", "-Command"]

script_dir    := justfile_directory()
target_dir    := script_dir / "target"
config        := script_dir / "uniffi-kmp.toml"
src_dir       := script_dir / "libkmp/mun-kmp/src"
jni_dir       := src_dir / "androidMain/jniLibs"
jvm_resources := src_dir / "jvmMain/resources"
artifact      := "mun"
tools         := script_dir / ".pixi/tools"

manifest := script_dir / "pixi.toml"
host_env := "pixi run --manifest-path " + quote(manifest) + " -e default"

python   := if os_family() == "windows" { "py -3" } else { "python3" }
run_py   := python + " " + quote(script_dir / "scripts/run_workflow.py")
workflow := if os() == "linux" { "pixi run --manifest-path " + quote(manifest) + " -e cross " + run_py } else { run_py }

gradlew := script_dir / "libkmp" / if os_family() == "windows" { "gradlew.bat" } else { "gradlew" }
gradle  := host_env + " " + quote(gradlew) + " -p " + quote(script_dir / "libkmp")

sdk_dir := script_dir / ".toolchains/android-sdk"
export ANDROID_HOME := if path_exists(sdk_dir) == "true" { sdk_dir } else { env("ANDROID_HOME", "") }

# Host library name and JNA resource dir (linux-x86-64, darwin-aarch64, win32-x86-64, ...)
host_lib := if os() == "windows" { artifact + ".dll" } else if os() == "macos" { "lib" + artifact + ".dylib" } else { "lib" + artifact + ".so" }
host_jna := (if os() == "macos" { "darwin" } else if os() == "windows" { "win32" } else { os() }) + "-" + (if arch() == "x86_64" { "x86-64" } else { arch() })

so_file  := "lib" + artifact + ".so"
dll_file := artifact + ".dll"
host_dir := ".:" + host_jna

android_libs := "aarch64-linux-android:arm64-v8a armv7-linux-androideabi:armeabi-v7a i686-linux-android:x86 x86_64-linux-android:x86_64"
linux_libs   := "x86_64-unknown-linux-gnu:linux-x86-64 aarch64-unknown-linux-gnu:linux-aarch64 armv7-unknown-linux-gnueabihf:linux-arm i686-unknown-linux-gnu:linux-x86 riscv64gc-unknown-linux-gnu:linux-riscv64 x86_64-unknown-linux-musl:linux-x86-64-musl aarch64-unknown-linux-musl:linux-aarch64-musl armv7-unknown-linux-musleabihf:linux-arm-musl i686-unknown-linux-musl:linux-x86-musl riscv64gc-unknown-linux-musl:linux-riscv64-musl x86_64-unknown-freebsd:freebsd-x86-64"
windows_libs := "x86_64-pc-windows-msvc:win32-x86-64 i686-pc-windows-msvc:win32-x86 aarch64-pc-windows-msvc:win32-aarch64"

[linux]
default: setup setup-cross build-android build-linux build-windows clean-kmp generate-bindings copy-android copy-linux copy-windows build-kmp

[macos]
default: setup build-host clean-kmp generate-bindings copy-host build-kmp

[windows]
default: setup build-host clean-kmp generate-bindings copy-host build-kmp

# Sub-target groups for Linux host cross-compilation
android: build-android copy-android
linux: build-linux copy-linux
windows: build-windows copy-windows

host: build-host clean-kmp generate-bindings copy-host

setup:
    pixi install --manifest-path "{{manifest}}" -e default
    rustup toolchain install --no-self-update

# Check / install the cross toolchain (NDK, SDK, LLVM source, Rust targets, cargo-xwin)
setup-cross: setup
    {{workflow}} setup

# Cross-compile the Rust library, building LLVM 14 first where it is missing (Linux x86_64 host)
build-android: setup setup-cross
    {{workflow}} build android

build-linux: setup setup-cross
    {{workflow}} build linux

build-windows: setup setup-cross
    {{workflow}} build windows

# Build LLVM 14 ahead of time for platforms or targets (default: all; build-* does it on demand)
[positional-arguments]
[unix]
build-llvm *targets: setup-cross
    {{workflow}} llvm "$@"

[windows]
build-llvm *targets: setup-cross
    {{workflow}} llvm {{targets}}

# Copy cross-compiled libraries into libkmp
copy-android: (_copy-libs jni_dir so_file android_libs)

copy-linux: (_copy-libs jvm_resources so_file linux_libs)

copy-windows: (_copy-libs jvm_resources dll_file windows_libs)

# Build library natively for current host
build-host: setup
    {{host_env}} cargo build --lib --release

# Copy host library binary to JVM resources directory
copy-host: (_copy-libs jvm_resources host_lib host_dir)

# Copies target/<dir>/release/<file> to <dest>/<subdir> for each "<dir>:<subdir>" pair
[unix]
_copy-libs dest file pairs:
    for pair in {{pairs}}; do \
        mkdir -p "{{dest}}/${pair#*:}"; \
        cp -v "{{target_dir}}/${pair%%:*}/release/{{file}}" "{{dest}}/${pair#*:}/"; \
    done

[windows]
_copy-libs dest file pairs:
    foreach ($pair in '{{pairs}}'.Split(' ')) { $dir, $sub = $pair.Split(':'); New-Item -ItemType Directory -Force -Path "{{dest}}/$sub" | Out-Null; Copy-Item -ErrorAction Stop -Path "{{target_dir}}/$dir/release/{{file}}" -Destination "{{dest}}/$sub/" }

# Clean generated KMP files
[unix]
clean-kmp:
    rm -rf "{{src_dir}}/androidMain" "{{src_dir}}/commonMain" "{{src_dir}}/nativeMain" "{{src_dir}}/jvmMain" \
           "{{src_dir}}/nativeInterop/cinterop/headers"

[windows]
clean-kmp:
    foreach ($d in "androidMain", "commonMain", "nativeMain", "jvmMain", "nativeInterop/cinterop/headers") { Remove-Item -Recurse -Force -ErrorAction Ignore "{{src_dir}}/$d" }

# Clean build artifacts and Gradle output (built LLVMs and toolchains are kept)
clean: clean-kmp
    cargo clean
    {{gradle}} clean

# Generate KMP Kotlin bindings from the unstripped debug host library
[unix]
generate-bindings: setup
    cargo install --locked --root "{{tools}}" gobley-uniffi-bindgen@0.3.7
    {{host_env}} cargo build --lib
    "{{tools}}/bin/gobley-uniffi-bindgen" \
        --library "{{target_dir}}/debug/{{host_lib}}" \
        --config  "{{config}}" \
        --out-dir "{{src_dir}}"

[windows]
generate-bindings: setup
    cargo install --locked --root "{{tools}}" gobley-uniffi-bindgen@0.3.7
    {{host_env}} cargo build --lib
    & "{{tools}}/bin/gobley-uniffi-bindgen.exe" `
        --library "{{target_dir}}/debug/{{host_lib}}" `
        --config  "{{config}}" `
        --out-dir "{{src_dir}}"

# Assemble the KMP library
build-kmp: setup
    {{gradle}} :mun-kmp:assemble --no-configuration-cache

# Publish the KMP library to the local Maven repository (~/.m2)
publish-local: setup
    {{gradle}} :mun-kmp:publishToMavenLocal

# Run all library tests that can execute on current host
test-kmp: test-kmp-jvm test-kmp-android

# Run library JVM tests
test-kmp-jvm: setup
    {{gradle}} :mun-kmp:jvmTest

# Run library Android host tests (runs on JVM, no device needed)
test-kmp-android: setup
    {{gradle}} :mun-kmp:testAndroidHostTest

# Run every library test
test-kmp-all: setup
    {{gradle}} :mun-kmp:allTests
