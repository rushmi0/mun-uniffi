#!/usr/bin/env python3

from __future__ import annotations

import sys
from pathlib import Path

from install_toolchain import (
    ANDROID_API, ARTIFACT, ROOT, BuildError, android_ndk_dir, build_and_install_llvm, capture,
    llvm_build_dir, llvm_cmake_args, llvm_needs_build, llvm_prefix, log, ndk_bin_dir,
    platform_main, require_llvm, run, write_file, write_llvm_config,
)

PLATFORM = "android"

ABIS = {
    "arm64-v8a": ("aarch64-linux-android", "aarch64-linux-android"),
    "armeabi-v7a": ("armv7-linux-androideabi", "armv7a-linux-androideabi"),
    "x86": ("i686-linux-android", "i686-linux-android"),
    "x86_64": ("x86_64-linux-android", "x86_64-linux-android"),
}

LLVM_CFLAGS = "-D_GNU_SOURCE -D__STDC_CONSTANT_MACROS -D__STDC_FORMAT_MACROS -D__STDC_LIMIT_MACROS"
LLVM_CXXFLAGS = f"-std=c++14 -fno-exceptions {LLVM_CFLAGS}"
LLVM_SYSTEM_LIBS = "-ldl -lm"


def resolve_abis(names: list[str]) -> list[str]:
    abi_of = {rust_target: abi for abi, (rust_target, _) in ABIS.items()}
    abis = []
    for name in names or ABIS:
        if name not in ABIS and name not in abi_of:
            raise BuildError(f"unknown Android ABI: {name} (known: {', '.join(ABIS)})")
        abis.append(abi_of.get(name, name))
    return abis


def ndk_clang(abi: str) -> Path:
    _, clang_triple = ABIS[abi]
    return ndk_bin_dir() / f"{clang_triple}{ANDROID_API}-clang"


def cmake_args(abi: str) -> list[str]:
    _, triple = ABIS[abi]
    toolchain_file = android_ndk_dir() / "build" / "cmake" / "android.toolchain.cmake"
    return llvm_cmake_args(llvm_build_dir(PLATFORM, abi), llvm_prefix(PLATFORM, abi), triple, "AArch64;ARM;X86") + [
        f"-DCMAKE_TOOLCHAIN_FILE={toolchain_file}",
        f"-DANDROID_ABI={abi}",
        f"-DANDROID_PLATFORM=android-{ANDROID_API}",
        "-DANDROID_STL=c++_static",
        "-DLLVM_ENABLE_LIBPFM=OFF",
    ]


def cargo_env(abi: str, env: dict) -> dict:
    rust_target, _ = ABIS[abi]
    clang = ndk_clang(abi)
    key = rust_target.replace("-", "_")
    return {
        **env,
        f"CC_{key}": str(clang),
        f"CXX_{key}": f"{clang}++",
        f"AR_{key}": str(ndk_bin_dir() / "llvm-ar"),
        f"CXXSTDLIB_{key}": "c++_static",
        f"CARGO_TARGET_{key.upper()}_LINKER": str(clang),
        "LLVM_SYS_140_PREFIX": str(llvm_prefix(PLATFORM, abi)),
    }


def cargo_command(abi: str, builtins: str) -> list[str]:
    rust_target, _ = ABIS[abi]
    return ["cargo", "rustc", "--lib", "--release", "--target", rust_target, "--", "-C", f"link-arg={builtins}"]


def finish_llvm_install(abi: str) -> None:
    prefix = llvm_prefix(PLATFORM, abi)
    write_llvm_config(prefix, LLVM_CFLAGS, LLVM_CXXFLAGS, "libLLVM*.a", LLVM_SYSTEM_LIBS)
    write_file(prefix / "lib" / "libstdc++.a", "INPUT(-lc++_static -lc++abi)\n")
    libffi = prefix / "lib" / "libffi.a"
    if not libffi.is_file():
        run([ndk_bin_dir() / "llvm-ar", "rc", libffi])


def build_llvm(abi: str, env: dict) -> None:
    if llvm_needs_build(llvm_prefix(PLATFORM, abi), f"Android {abi}"):
        build_and_install_llvm(cmake_args(abi), llvm_build_dir(PLATFORM, abi), env)
    finish_llvm_install(abi)


def build_library(abi: str, env: dict) -> None:
    require_llvm(llvm_prefix(PLATFORM, abi), f"python3 scripts/build_llvm_android.py llvm {abi}")
    builtins = capture([ndk_clang(abi), "-print-libgcc-file-name"], env=env)
    log(f"building lib{ARTIFACT}.so for Android {abi}")
    run(cargo_command(abi, builtins), env=cargo_env(abi, env), cwd=ROOT)


def main(argv: list[str] | None = None) -> int:
    return platform_main(__file__, __doc__, argv, resolve_abis, build_llvm, build_library)


if __name__ == "__main__":
    sys.exit(main())
