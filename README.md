# mun-uniffi

A template for integrating Rust with Kotlin Multiplatform (KMP) using [UniFFI](https://mozilla.github.io/uniffi-rs/) and [gobley](https://gobley.dev/).

It embeds the [Mun](https://mun-lang.org/) compiler and runtime in a Rust crate and exposes them to Kotlin as `MunEngine`, a small `eval()`-style script engine. A single `justfile` drives the whole pipeline: cross-compilation, binding generation, packaging and publishing.

## Supported Platforms

* JVM
* Android
* iOS
* Linux
* macOS
* Windows

## Architecture

```text
 Kotlin app (JVM / Android)
        │  MunEngine.eval() / call()
        ▼
 libkmp/mun-kmp            KMP library: generated bindings + bundled native libs
        │  JNA (jvmMain) / JNI libs (androidMain)
        ▼
 libmun.so / mun.dll       cdylib built from this crate (src/)
        │  UniFFI scaffolding (#[uniffi::export])
        ▼
 src/engine.rs             MunEngine: compile, load, hot-reload, call
        │
        ▼
 ../crates/mun_compiler    Mun compiler + runtime (path deps)
 ../crates/mun_runtime     ── statically link LLVM 14 + LLD
```

### Build pipeline

```text
 build-<platform>   cargo build per Rust target ──► target/<triple>/release/libmun.*
        │                (LLVM 14 built first where missing)
 generate-bindings  gobley-uniffi-bindgen on the debug host lib ──► libkmp/mun-kmp/src/*Main
        │
 copy-<platform>    native libs ──► androidMain/jniLibs/<abi>, jvmMain/resources/<jna-dir>
        │
 build-kmp          Gradle :mun-kmp:assemble ──► KMP library (publish-local ──► ~/.m2)
```

### Layout

| Path | Contents |
|------|----------|
| `src/lib.rs`, `src/engine.rs` | Rust crate exported through UniFFI (`MunEngine`, `MunValue`, `MunException`) |
| `uniffi-kmp.toml` | gobley binding configuration |
| `justfile` | Every build, test and publish recipe |
| `pixi.toml` | Host (`default`) and cross-compilation (`cross`) environments, plus task shortcuts |
| `rust-toolchain.toml` | Pinned Rust `1.89.0` and all cross targets |
| `scripts/run_workflow.py` | Entry point for `setup`, `build` and `llvm` on Linux hosts |
| `scripts/install_toolchain.py` | Downloads NDK, SDK, LLVM source and cargo-xwin into `.toolchains/` |
| `scripts/build_llvm_{android,linux,windows}.py` | Per-platform LLVM 14 and Rust library builds |
| `libkmp/` | Gradle project; `mun-kmp/src` holds generated `commonMain`, `jvmMain`, `androidMain`, `nativeMain` and hand-written `commonTest` |

## Supported Platforms

| Platform | Targets | Built on |
|----------|---------|----------|
| Android (API 24) | `arm64-v8a`, `armeabi-v7a`, `x86`, `x86_64` | Linux x86_64 |
| Linux glibc | x86_64, aarch64, armv7, i686, riscv64 | Linux x86_64 |
| Linux musl | x86_64, aarch64, armv7, i686, riscv64 | Linux x86_64 |
| FreeBSD | x86_64 | Linux x86_64 |
| Windows MSVC | x86_64, i686, aarch64 | Linux x86_64 (cross) or Windows (host) |
| macOS | host arch only | macOS |

iOS is not supported yet (see [Limitations](#limitations)).

## Prerequisites

* [just](https://just.systems): runs every command below.
* [pixi](https://pixi.sh) (`curl -fsSL https://pixi.sh/install.sh | sh`): the recipes call it to provide everything else except Rust (`pixi.toml`): JDK 21 and LLVM 14 (`llvmdev`) on Linux, macOS and Windows, plus zig, clang-cl, cmake and ninja for the Linux cross builds.
* [rustup](https://rustup.rs): Rust is pinned to `1.89.0` by `rust-toolchain.toml`, which also lists every cross target.
* A host C compiler (`gcc` or `clang`) for cargo build scripts.

No containers, Android Studio or system packages are needed.

The crate depends on the Mun crates by path (`../crates/*`), so it has to sit inside a Mun checkout.

## Getting Started

```bash
just setup-cross   # Linux, once: LLVM 14 source, Android NDK r30 + SDK, cargo-xwin
just               # full pipeline for this host
```

On Linux x86_64 the full pipeline cross-builds Android, Linux (glibc + musl), FreeBSD and Windows, regenerates the bindings and assembles the KMP library. On macOS and Windows it builds that host's library with plain cargo.

To build only the host library and run the tests:

```bash
just host
just test-kmp
```

## Command List

Recipes from the `justfile`. Run one with `just <recipe>`; `just --list` prints them all.

### Pipelines

| Recipe | Description |
|--------|-------------|
| `default` | Full pipeline for this host (Linux: every cross target; macOS/Windows: host only). Runs when you type plain `just` |
| `host` | Build the host library, regenerate bindings and copy it into `libkmp` |
| `android` | `build-android` + `copy-android` |
| `linux` | `build-linux` + `copy-linux` (glibc, musl, FreeBSD) |
| `windows` | `build-windows` + `copy-windows` |

### Setup

| Recipe | Description |
|--------|-------------|
| `setup` | Install the pixi `default` environment and the pinned Rust toolchain |
| `setup-cross` | Check / install the cross toolchain (NDK, SDK, LLVM source, Rust targets, cargo-xwin) |
| `build-llvm [target...]` | Build LLVM 14 ahead of time for platforms or targets (default: all) |

### Build

| Recipe | Description |
|--------|-------------|
| `build-host` | `cargo build --lib --release` for the host |
| `build-android` | Cross-compile for all Android ABIs |
| `build-linux` | Cross-compile for Linux glibc/musl and FreeBSD |
| `build-windows` | Cross-compile for Windows MSVC |
| `generate-bindings` | Regenerate the Kotlin bindings from the current Rust API |
| `copy-host` | Copy the host library into `jvmMain/resources` |
| `copy-android` | Copy the Android libraries into `androidMain/jniLibs` |
| `copy-linux` | Copy the Linux and FreeBSD libraries into `jvmMain/resources` |
| `copy-windows` | Copy the Windows libraries into `jvmMain/resources` |
| `build-kmp` | Assemble the KMP library with Gradle |

### Test & Publish

| Recipe | Description |
|--------|-------------|
| `test-kmp` | JVM and Android host tests |
| `test-kmp-jvm` | JVM tests only |
| `test-kmp-android` | Android host tests (on the JVM, no device needed) |
| `test-kmp-all` | Every Gradle test task (`allTests`) |
| `publish-local` | Publish to the local Maven repository (`~/.m2`) |

### Clean

| Recipe | Description |
|--------|-------------|
| `clean-kmp` | Remove generated KMP sources |
| `clean` | `clean-kmp` + `cargo clean` + Gradle clean (built LLVMs and toolchains are kept) |

## Toolchains

`just setup-cross` downloads into `.toolchains/` (gitignored, kept by `pixi clean`), with checksums verified:

| What | Where | Source |
|------|-------|--------|
| LLVM 14.0.6 source (+ `<cstdint>` patch) | `.toolchains/src` | GitHub release |
| Android NDK r30 | `.toolchains/android-ndk-r30` | [NDK downloads](https://developer.android.com/ndk/downloads) |
| Android SDK (platform + build-tools for Gradle) | `.toolchains/android-sdk` | command-line tools + `sdkmanager` |
| cargo-xwin | `.pixi/tools/bin` | `cargo install --locked` |

### LLVM 14 for every target

The Mun compiler links LLVM 14 and LLD statically, so every target needs its own LLVM 14. conda-forge's `llvmdev` only exists for host platforms (no Android, musl, armv7, i686, riscv64, FreeBSD or Windows arm64 builds). The `build-*` recipes therefore build a missing target's LLVM from source on first use, about 20-30 minutes per target, with the same toolchain the library is linked with, and set `LLVM_SYS_140_PREFIX` per target:

| Targets | Toolchain | Installed to |
|---------|-----------|--------------|
| Android (API 24) | NDK r30 clang | `.toolchains/llvm14/android/<abi>` |
| Linux glibc/musl, FreeBSD | zig (glibc 2.17, riscv64 2.27) | `.toolchains/llvm14/linux/<target>` |
| Windows MSVC | clang-cl + cargo-xwin | `.toolchains/llvm14/windows/<target>` |

Locations can be overridden through the environment (`TOOLCHAINS_DIR`, `LLVM14_ROOT`, `NDK_DIR`, `SDK_DIR`, ... see the top of `scripts/install_toolchain.py`), for example to reuse existing builds:

```bash
LLVM14_ROOT=/path/to/llvm14 NDK_DIR=/path/to/android-ndk-r30 just
```

## Mun Script Engine

`MunEngine` (see `src/engine.rs`) compiles and runs Mun source from Kotlin:

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
