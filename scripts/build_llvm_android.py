#!/usr/bin/env python3
"""Android: cross-build LLVM 14 + lld per ABI, then cross-compile the Rust library with the NDK.

    python3 scripts/build_llvm_android.py llvm [abi ...]   # LLVM 14 + lld (slow, once per ABI)
    python3 scripts/build_llvm_android.py lib  [abi ...]   # cargo build of lib<artifact>.so
    python3 scripts/build_llvm_android.py all  [abi ...]   # both, in order (LLVM only if missing)

ABIs: arm64-v8a armeabi-v7a x86 x86_64 (default: all four); Rust targets work too.
LLVM is installed into .toolchains/llvm14/android/<abi>, which `lib` passes to
llvm-sys as LLVM_SYS_140_PREFIX. Output: target/<rust target>/release/lib<artifact>.so
(the justfile copies it into libkmp).
"""

from __future__ import annotations

import argparse
import sys

from install_toolchain import (
    ANDROID_API, ARTIFACT, ROOT, BuildError, android_ndk_dir, build_and_install_llvm,
    build_env, capture, ensure_cross_env, llvm_build_dir, llvm_cmake_args, llvm_installed,
    llvm_prefix, log, ndk_bin_dir, require_llvm, require_llvm_source, run, write_file,
    write_llvm_config,
)

# ABI -> (Rust target, clang / LLVM triple)
ABIS = {
    "arm64-v8a": ("aarch64-linux-android", "aarch64-linux-android"),
    "armeabi-v7a": ("armv7-linux-androideabi", "armv7a-linux-androideabi"),
    "x86": ("i686-linux-android", "i686-linux-android"),
    "x86_64": ("x86_64-linux-android", "x86_64-linux-android"),
}

LLVM_CFLAGS = "-D_GNU_SOURCE -D__STDC_CONSTANT_MACROS -D__STDC_FORMAT_MACROS -D__STDC_LIMIT_MACROS"
LLVM_CXXFLAGS = f"-std=c++14 -fno-exceptions {LLVM_CFLAGS}"


def resolve_abis(names: list[str]) -> list[str]:
    """Map the command-line names (ABIs or Rust targets) to ABIs."""
    if not names:
        return list(ABIS)
    by_rust_target = {rust_target: abi for abi, (rust_target, _) in ABIS.items()}
    abis = []
    for name in names:
        abi = name if name in ABIS else by_rust_target.get(name)
        if abi is None:
            raise BuildError(f"unknown Android ABI: {name} (known: {', '.join(ABIS)})")
        abis.append(abi)
    return abis


# ------------------------------------------------------------------ LLVM


def cmake_args(abi: str) -> list[str]:
    _, triple = ABIS[abi]
    build = llvm_build_dir("android", abi)
    prefix = llvm_prefix("android", abi)
    return llvm_cmake_args(build, prefix, triple, "AArch64;ARM;X86") + [
        f"-DCMAKE_TOOLCHAIN_FILE={android_ndk_dir() / 'build' / 'cmake' / 'android.toolchain.cmake'}",
        f"-DANDROID_ABI={abi}",
        f"-DANDROID_PLATFORM=android-{ANDROID_API}",
        "-DANDROID_STL=c++_static",
        "-DLLVM_ENABLE_LIBPFM=OFF",
    ]


def finish_llvm_install(abi: str) -> None:
    """Make the install usable from the host: llvm-config stand-in and link stubs."""
    prefix = llvm_prefix("android", abi)
    write_llvm_config(prefix, LLVM_CFLAGS, LLVM_CXXFLAGS, "libLLVM*.a", "-ldl -lm")
    # llvm-sys and lld_rs assume a glibc host: they link `stdc++` and `ffi`.
    # Redirect the former to the NDK's static libc++ and satisfy the latter
    # with an empty archive (LLVM is built without libffi).
    write_file(prefix / "lib" / "libstdc++.a", "INPUT(-lc++_static -lc++abi)\n")
    libffi = prefix / "lib" / "libffi.a"
    if not libffi.is_file():
        run([ndk_bin_dir() / "llvm-ar", "rc", libffi])


def build_llvm(abi: str, env: dict) -> None:
    prefix = llvm_prefix("android", abi)
    if llvm_installed(prefix):
        log(f"LLVM 14 for {abi} already installed: {prefix}")
    else:
        require_llvm_source()
        log(f"building LLVM 14 + lld for Android {abi} (slow, once per ABI)")
        build_and_install_llvm(cmake_args(abi), llvm_build_dir("android", abi), env)
    finish_llvm_install(abi)


# ------------------------------------------------------------------ Rust library


def ndk_clang(abi: str):
    _, clang_triple = ABIS[abi]
    return ndk_bin_dir() / f"{clang_triple}{ANDROID_API}-clang"


def cargo_env(abi: str, env: dict) -> dict:
    """Cross-compile environment for one ABI.

    Plain cargo instead of cargo-ndk: cargo-ndk sets CFLAGS_<target>, which hides
    the LLVM include flags llvm-sys passes to cc through CFLAGS.
    """
    rust_target, _ = ABIS[abi]
    clang = ndk_clang(abi)
    key = rust_target.replace("-", "_")
    env = dict(env)
    env[f"CC_{key}"] = str(clang)
    env[f"CXX_{key}"] = f"{clang}++"
    env[f"AR_{key}"] = str(ndk_bin_dir() / "llvm-ar")
    env[f"CXXSTDLIB_{key}"] = "c++_static"
    env[f"CARGO_TARGET_{key.upper()}_LINKER"] = str(clang)
    env["LLVM_SYS_140_PREFIX"] = str(llvm_prefix("android", abi))
    return env


def cargo_command(abi: str, builtins: str) -> list[str]:
    # rustc links with -nodefaultlibs, which drops compiler-rt; LLVM needs its
    # __clear_cache, which is not in Rust's compiler_builtins.
    rust_target, _ = ABIS[abi]
    return ["cargo", "rustc", "--lib", "--release", "--target", rust_target, "--", "-C", f"link-arg={builtins}"]


def build_library(abi: str, env: dict) -> None:
    require_llvm(llvm_prefix("android", abi), f"python3 scripts/build_llvm_android.py llvm {abi}")
    builtins = capture([ndk_clang(abi), "-print-libgcc-file-name"], env=env)
    log(f"building lib{ARTIFACT}.so for Android {abi}")
    run(cargo_command(abi, builtins), env=cargo_env(abi, env), cwd=ROOT)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage", choices=["llvm", "lib", "all"])
    parser.add_argument("abis", nargs="*", metavar="abi")
    args = parser.parse_args(argv)
    ensure_cross_env(__file__, argv)

    try:
        abis = resolve_abis(args.abis)
        env = build_env()
        if args.stage in ("llvm", "all"):
            for abi in abis:
                build_llvm(abi, env)
        if args.stage in ("lib", "all"):
            for abi in abis:
                build_library(abi, env)
    except BuildError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
