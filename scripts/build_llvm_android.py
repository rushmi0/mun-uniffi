#!/usr/bin/env python3

from __future__ import annotations

import sys
from pathlib import Path

from toolchain import (
    ANDROID_API, ARTIFACT, GNU_CFLAGS, GNU_CXXFLAGS, ROOT, BuildError, base_cmake_args, build_and_install,
    build_dir_for, capture, log, ndk_bin_dir, ndk_dir, needs_build, prefix_for, require_installed, run,
    run_platform_build, write_file, write_llvm_config,
)

PLATFORM = "android"

ABIS = {
    "arm64-v8a": ("aarch64-linux-android", "aarch64-linux-android"),
    "armeabi-v7a": ("armv7-linux-androideabi", "armv7a-linux-androideabi"),
    "x86": ("i686-linux-android", "i686-linux-android"),
    "x86_64": ("x86_64-linux-android", "x86_64-linux-android"),
}

LLVM_SYSTEM_LIBS = "-ldl -lm"


def resolve_abis(names: list[str]) -> list[str]:
    """Map ABI names or Rust target names to ABI names; an empty request selects every ABI."""
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
    toolchain_file = ndk_dir() / "build" / "cmake" / "android.toolchain.cmake"
    return base_cmake_args(build_dir_for(PLATFORM, abi), prefix_for(PLATFORM, abi), triple) + [
        f"-DCMAKE_TOOLCHAIN_FILE={toolchain_file}",
        f"-DANDROID_ABI={abi}",
        f"-DANDROID_PLATFORM=android-{ANDROID_API}",
        "-DANDROID_STL=c++_static",
        "-DLLVM_ENABLE_LIBPFM=OFF",
    ]


def cargo_env(abi: str, env: dict) -> dict:
    """Environment for cargo: per-target compiler, archiver and LLVM prefix settings."""
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
        "LLVM_SYS_140_PREFIX": str(prefix_for(PLATFORM, abi)),
    }


def cargo_command(abi: str, builtins: str) -> list[str]:
    rust_target, _ = ABIS[abi]
    return ["cargo", "rustc", "--lib", "--release", "--target", rust_target, "--", "-C", f"link-arg={builtins}"]


def finish_llvm_install(abi: str) -> None:
    """Write the llvm-config stand-in and the libraries llvm-sys expects to find."""
    prefix = prefix_for(PLATFORM, abi)
    write_llvm_config(prefix, GNU_CFLAGS, GNU_CXXFLAGS, "libLLVM*.a", LLVM_SYSTEM_LIBS)
    write_file(prefix / "lib" / "libstdc++.a", "INPUT(-lc++_static -lc++abi)\n")
    libffi = prefix / "lib" / "libffi.a"
    if not libffi.is_file():
        run([ndk_bin_dir() / "llvm-ar", "rc", libffi])


def build_llvm(abi: str, env: dict) -> None:
    prefix = prefix_for(PLATFORM, abi)
    if needs_build(prefix, f"Android {abi}"):
        build_and_install(cmake_args(abi), build_dir_for(PLATFORM, abi), env)
    finish_llvm_install(abi)


def build_library(abi: str, env: dict) -> None:
    require_installed(prefix_for(PLATFORM, abi), f"python3 scripts/build_llvm_android.py llvm {abi}")
    builtins = capture([ndk_clang(abi), "-print-libgcc-file-name"], env=env)
    log(f"building lib{ARTIFACT}.so for Android {abi}")
    run(cargo_command(abi, builtins), env=cargo_env(abi, env), cwd=ROOT)


def main(argv: list[str] | None = None) -> int:
    return run_platform_build(__file__, __doc__, argv, resolve_abis, build_llvm, build_library)


if __name__ == "__main__":
    sys.exit(main())
