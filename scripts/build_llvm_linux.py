#!/usr/bin/env python3

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from toolchain import (
    ARTIFACT, GNU_CFLAGS, GNU_CXXFLAGS, ROOT, BuildError, base_cmake_args, build_and_install, build_dir_for,
    log, needs_build, prefix_for, require_installed, run, run_platform_build, select_targets, write_file,
    write_llvm_config,
)

PLATFORM = "linux"

# glibc versions to link against. cargo-zigbuild takes the version as a suffix of the Rust target.
GLIBC = os.environ.get("GLIBC", "2.17")
GLIBC_RISCV64 = os.environ.get("GLIBC_RISCV64", "2.27")

# Rust target -> zig target, LLVM CPU flags and the values CMake needs for the system.
TARGETS = {
    "x86_64-unknown-linux-gnu":       {"zig": "x86_64-linux-gnu",     "cpu": "",                                       "system": "Linux",   "processor": "x86_64"},
    "aarch64-unknown-linux-gnu":      {"zig": "aarch64-linux-gnu",    "cpu": "",                                       "system": "Linux",   "processor": "aarch64"},
    "armv7-unknown-linux-gnueabihf":  {"zig": "arm-linux-gnueabihf",  "cpu": "-mcpu=generic+v7a+vfp3-d32+thumb2-neon", "system": "Linux",   "processor": "armv7"},
    "i686-unknown-linux-gnu":         {"zig": "x86-linux-gnu",        "cpu": "-mcpu=pentium4",                         "system": "Linux",   "processor": "i686"},
    "riscv64gc-unknown-linux-gnu":    {"zig": "riscv64-linux-gnu",    "cpu": "-mcpu=generic_rv64+m+a+f+d+c",           "system": "Linux",   "processor": "riscv64"},
    "x86_64-unknown-linux-musl":      {"zig": "x86_64-linux-musl",    "cpu": "",                                       "system": "Linux",   "processor": "x86_64"},
    "aarch64-unknown-linux-musl":     {"zig": "aarch64-linux-musl",   "cpu": "",                                       "system": "Linux",   "processor": "aarch64"},
    "armv7-unknown-linux-musleabihf": {"zig": "arm-linux-musleabihf", "cpu": "-mcpu=generic+v7a+vfp3-d32+thumb2-neon", "system": "Linux",   "processor": "armv7"},
    "i686-unknown-linux-musl":        {"zig": "x86-linux-musl",       "cpu": "-mcpu=pentium4",                         "system": "Linux",   "processor": "i686"},
    "riscv64gc-unknown-linux-musl":   {"zig": "riscv64-linux-musl",   "cpu": "-mcpu=generic_rv64+m+a+f+d+c",           "system": "Linux",   "processor": "riscv64"},
    "x86_64-unknown-freebsd":         {"zig": "x86_64-freebsd",       "cpu": "",                                       "system": "FreeBSD", "processor": "x86_64"},
}

LLVM_SYSTEM_LIBS = "-lpthread -ldl -lm"
LIBCXX_ARCHIVES = ("libc++.a", "libc++abi.a")

# Compiler wrapper for `zig cc` / `zig c++`. Compile-only modes (-c, -S, -E) pass
# their arguments through unchanged; in every other mode the wrapper drops -std=* flags.
ZIG_COMPILER_WRAPPER = """#!/bin/sh
case " $* " in
    *" -c "*|*" -S "*|*" -E "*) ;;
    *) for arg; do shift; case "$arg" in -std=*) ;; *) set -- "$@" "$arg" ;; esac; done ;;
esac
exec "{zigbuild}" zig {tool} -- -target {zig_target} {cpu} "$@"
"""

# Wrapper for `ar` and `ranlib`, which are plain zig subcommands.
ZIG_TOOL_WRAPPER = """#!/bin/sh
exec zig {tool} "$@"
"""


def resolve_targets(names: list[str]) -> list[str]:
    return select_targets(names, list(TARGETS), "Linux")


def glibc_suffix(target: str) -> str:
    """The glibc version suffix for `target`, or "" for targets that are not glibc-based."""
    if target.startswith("riscv64gc-") and target.endswith("-gnu"):
        return f".{GLIBC_RISCV64}"
    if target.endswith(("-gnu", "-gnueabihf")):
        return f".{GLIBC}"
    return ""


def zig_target(target: str) -> str:
    return TARGETS[target]["zig"] + glibc_suffix(target)


def cargo_command(target: str) -> list[str]:
    return ["cargo", "zigbuild", "--lib", "--release", "--target", target + glibc_suffix(target)]


# ---------------------------------------------------------------------------
# Compiler wrappers
# ---------------------------------------------------------------------------

def zig_wrapper_signature(target: str, zigbuild: str) -> str:
    """Text that identifies the wrapper configuration for `target`.

    Stored next to the wrappers so a change of target or zig path forces a clean rebuild.
    """
    return f'exec "{zigbuild}" zig $tool -- -target {zig_target(target)} {TARGETS[target]["cpu"]}\n'


def zig_wrappers(target: str, zigbuild: str) -> dict[str, str]:
    """File name -> content of each wrapper CMake uses to build LLVM for `target`."""
    compiler = {
        tool: ZIG_COMPILER_WRAPPER.format(zigbuild=zigbuild, tool=tool, zig_target=zig_target(target),
                                          cpu=TARGETS[target]["cpu"])
        for tool in ("cc", "c++")
    }
    return compiler | {tool: ZIG_TOOL_WRAPPER.format(tool=tool) for tool in ("ar", "ranlib")}


def write_zig_wrappers(target: str, build: Path, zigbuild: str) -> None:
    signature_file = build / "zig" / "target"
    signature = zig_wrapper_signature(target, zigbuild)
    if signature_file.is_file() and signature_file.read_text() != signature:
        log(f"zig target changed, removing old build dir {build}")
        shutil.rmtree(build)
    write_file(signature_file, signature)
    for name, text in zig_wrappers(target, zigbuild).items():
        write_file(build / "zig" / name, text, executable=True)


# ---------------------------------------------------------------------------
# CMake and installation
# ---------------------------------------------------------------------------

def cmake_args(target: str) -> list[str]:
    info = TARGETS[target]
    build = build_dir_for(PLATFORM, target)
    zig_dir = build / "zig"
    args = base_cmake_args(build, prefix_for(PLATFORM, target), target) + [
        f"-DCMAKE_SYSTEM_NAME={info['system']}",
        f"-DCMAKE_SYSTEM_PROCESSOR={info['processor']}",
        f"-DCMAKE_C_COMPILER={zig_dir / 'cc'}",
        f"-DCMAKE_CXX_COMPILER={zig_dir / 'c++'}",
        f"-DCMAKE_AR={zig_dir / 'ar'}",
        f"-DCMAKE_RANLIB={zig_dir / 'ranlib'}",
        "-DCMAKE_C_LINKER_DEPFILE_SUPPORTED=FALSE",
        "-DCMAKE_CXX_LINKER_DEPFILE_SUPPORTED=FALSE",
        "-DLLVM_ENABLE_BACKTRACES=OFF",
        "-DLLVM_ENABLE_LIBPFM=OFF",
    ]
    if "-musl" in target:
        args.append("-DHAVE_LSEEK64=0")
    return args


def find_archive(compiler_output: str, archive: str) -> str | None:
    return next((word for word in compiler_output.split() if word.endswith(f"/{archive}")), None)


def copy_zig_libcxx(target: str, zigbuild: str, env: dict) -> None:
    """Copy zig's libc++ archives into the LLVM prefix, found by asking zig to link a probe."""
    dest = prefix_for(PLATFORM, target) / "libcxx"
    if all((dest / archive).is_file() for archive in LIBCXX_ARCHIVES):
        return
    log(f"locating zig's libc++ for {target}")
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "cxx-probe.cpp"
        probe.write_text("int main(){}\n")
        cmd = [zigbuild, "zig", "c++", "--", "-target", zig_target(target), *TARGETS[target]["cpu"].split(),
               "-v", str(probe), "-o", str(Path(tmp) / "cxx-probe")]
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    dest.mkdir(parents=True, exist_ok=True)
    for archive in LIBCXX_ARCHIVES:
        path = find_archive(result.stdout + result.stderr, archive)
        if path is None:
            raise BuildError(f"could not locate zig's {archive} for {target}:\n{result.stderr[-2000:]}")
        shutil.copy2(path, dest / archive)


def finish_llvm_install(target: str, zigbuild: str, env: dict) -> None:
    """Write the llvm-config stand-in and the placeholder libraries llvm-sys expects."""
    prefix = prefix_for(PLATFORM, target)
    write_llvm_config(prefix, GNU_CFLAGS, GNU_CXXFLAGS, "libLLVM*.a", LLVM_SYSTEM_LIBS)
    write_file(prefix / "lib" / "libffi.so", "INPUT()\n")
    if "-musl" in target:
        for lib in ("pthread", "dl", "m", "c++"):
            write_file(prefix / "lib" / f"lib{lib}.a", "!<arch>\n")
    copy_zig_libcxx(target, zigbuild, env)


# ---------------------------------------------------------------------------
# Build stages
# ---------------------------------------------------------------------------

def zigbuild_path(env: dict) -> str:
    path = os.environ.get("ZIGBUILD") or shutil.which("cargo-zigbuild", path=env["PATH"])
    if not path:
        raise BuildError("cargo-zigbuild not found (it comes from the pixi `cross` env)")
    return path


def build_llvm(target: str, env: dict) -> None:
    zigbuild = zigbuild_path(env)
    prefix = prefix_for(PLATFORM, target)
    if needs_build(prefix, target):
        build = build_dir_for(PLATFORM, target)
        write_zig_wrappers(target, build, zigbuild)
        build_and_install(cmake_args(target), build, env)
    finish_llvm_install(target, zigbuild, env)


def build_library(target: str, env: dict) -> None:
    prefix = prefix_for(PLATFORM, target)
    require_installed(prefix, f"python3 scripts/build_llvm_linux.py llvm {target}")
    log(f"building lib{ARTIFACT}.so for {target}")
    run(cargo_command(target), env={**env, "LLVM_SYS_140_PREFIX": str(prefix)}, cwd=ROOT)


def main(argv: list[str] | None = None) -> int:
    return run_platform_build(__file__, __doc__, argv, resolve_targets, build_llvm, build_library)


if __name__ == "__main__":
    sys.exit(main())
