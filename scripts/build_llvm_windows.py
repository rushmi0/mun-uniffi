#!/usr/bin/env python3

from __future__ import annotations

import os
import shlex
import shutil
import sys
from pathlib import Path

from install_toolchain import (
    ARTIFACT, ROOT, build_and_install_llvm, capture, llvm_build_dir, llvm_cmake_args, llvm_needs_build,
    llvm_prefix, log, platform_main, require_llvm, run, select_targets, write_file, write_llvm_config,
)

PLATFORM = "windows"
TARGETS = ["x86_64-pc-windows-msvc", "i686-pc-windows-msvc", "aarch64-pc-windows-msvc"]

XWIN_ARCH = os.environ.get("XWIN_ARCH", "x86,x86_64,aarch64")
XWIN_CACHE = Path(os.environ.get("XWIN_CACHE", Path.home() / ".cache" / "cargo-xwin"))

# On MSVC, llvm-sys and lld_rs expect `.lib` names from --libnames and --system-libs.
LLVM_CFLAGS = (
    "-D_CRT_SECURE_NO_DEPRECATE -D_CRT_SECURE_NO_WARNINGS "
    "-D__STDC_CONSTANT_MACROS -D__STDC_FORMAT_MACROS -D__STDC_LIMIT_MACROS"
)
LLVM_SYSTEM_LIBS = "psapi.lib shell32.lib ole32.lib uuid.lib advapi32.lib"

def resolve_targets(names: list[str]) -> list[str]:
    return select_targets(names, TARGETS, "Windows")


def cmake_args(target: str) -> list[str]:
    toolchain_file = XWIN_CACHE / "cmake" / "clang-cl" / f"{target}-toolchain.cmake"
    build = llvm_build_dir(PLATFORM, target)
    return llvm_cmake_args(build, llvm_prefix(PLATFORM, target), target, "AArch64;ARM;X86") + [
        f"-DCMAKE_MAKE_PROGRAM={shutil.which('ninja') or 'ninja'}",
        f"-DCMAKE_TOOLCHAIN_FILE={toolchain_file}",
        "-DCMAKE_RC_COMPILER=llvm-rc",
        "-DCMAKE_MT=llvm-mt",
        "-DLLVM_USE_CRT_RELEASE=MD",
        "-DLLVM_BUILD_LLVM_C_DYLIB=OFF",
        "-DLLVM_ENABLE_DIA_SDK=OFF",
    ]


def parse_xwin_env(output: str) -> dict[str, str]:
    """`cargo xwin env` prints lines like:  export NAME="value";"""
    parsed = {}
    for line in output.splitlines():
        words = shlex.split(line.strip().rstrip(";"))
        if len(words) == 2 and words[0] == "export" and "=" in words[1]:
            name, value = words[1].split("=", 1)
            parsed[name] = value
    return parsed


def xwin_base_env(env: dict) -> dict:
    env = {**env, "XWIN_ARCH": XWIN_ARCH}
    if real_clang := shutil.which("clang", path="/usr/local/bin:/usr/bin:/bin"):
        env["LUNA_REAL_CLANG"] = real_clang
    return env


def with_llvm_flags(env: dict, target: str, llvm_cflags: str, llvm_cxxflags: str) -> dict:
    key = target.replace("-", "_")
    return {
        **env,
        f"CFLAGS_{key}": f"{env.get(f'CFLAGS_{key}', '')} {llvm_cflags}",
        f"CXXFLAGS_{key}": f"{env.get(f'CXXFLAGS_{key}', '')} {llvm_cxxflags}",
        "LLVM_SYS_140_PREFIX": str(llvm_prefix(PLATFORM, target)),
    }


def remove_stale_xwin_clang_link() -> None:
    link = XWIN_CACHE / "clang-cl"
    if link.is_symlink() and not link.exists():
        link.unlink()


def xwin_env(target: str, env: dict) -> dict[str, str]:
    """Also downloads the MSVC CRT + Windows SDK if needed and writes the clang-cl
    CMake toolchain file for `target`."""
    remove_stale_xwin_clang_link()
    return parse_xwin_env(capture(["cargo", "xwin", "env", "--target", target], env=env))


def cargo_env(target: str, env: dict) -> dict:
    env = xwin_base_env(env)
    env |= xwin_env(target, env)
    llvm_config = llvm_prefix(PLATFORM, target) / "bin" / "llvm-config"
    return with_llvm_flags(env, target, capture([llvm_config, "--cflags"]), capture([llvm_config, "--cxxflags"]))


def finish_llvm_install(target: str) -> None:
    prefix = llvm_prefix(PLATFORM, target)
    write_llvm_config(prefix, LLVM_CFLAGS, LLVM_CFLAGS, "LLVM*.lib", LLVM_SYSTEM_LIBS)
    # inkwell and lld_rs check the host OS, not the target, so from Linux they ask
    # for ffi.lib. LLVM is built without libffi, so an empty archive satisfies them.
    write_file(prefix / "lib" / "ffi.lib", "!<arch>\n")


def build_llvm(target: str, env: dict) -> None:
    if llvm_needs_build(llvm_prefix(PLATFORM, target), target):
        llvm_env = xwin_base_env(env)
        xwin_env(target, llvm_env)
        build_and_install_llvm(cmake_args(target), llvm_build_dir(PLATFORM, target), llvm_env)
    finish_llvm_install(target)


def build_library(target: str, env: dict) -> None:
    require_llvm(llvm_prefix(PLATFORM, target), f"python3 scripts/build_llvm_windows.py llvm {target}")
    log(f"building {ARTIFACT}.dll for {target}")
    run(["cargo", "build", "--lib", "--release", "--target", target], env=cargo_env(target, env), cwd=ROOT)


def main(argv: list[str] | None = None) -> int:
    return platform_main(__file__, __doc__, argv, resolve_targets, build_llvm, build_library)


if __name__ == "__main__":
    sys.exit(main())
