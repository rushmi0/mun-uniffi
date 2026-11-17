#!/usr/bin/env python3

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from install_toolchain import (
    ARTIFACT, ROOT, BuildError, build_and_install_llvm, llvm_build_dir, llvm_cmake_args,
    llvm_needs_build, llvm_prefix, log, platform_main, require_llvm, run, select_targets,
    write_file, write_llvm_config,
)

PLATFORM = "linux"
GLIBC = os.environ.get("GLIBC", "2.17")
GLIBC_RISCV64 = os.environ.get("GLIBC_RISCV64", "2.27")

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

LLVM_CFLAGS = "-D_GNU_SOURCE -D__STDC_CONSTANT_MACROS -D__STDC_FORMAT_MACROS -D__STDC_LIMIT_MACROS"
LLVM_CXXFLAGS = f"-std=c++14 -fno-exceptions {LLVM_CFLAGS}"
LLVM_SYSTEM_LIBS = "-lpthread -ldl -lm"
LIBCXX_ARCHIVES = ("libc++.a", "libc++abi.a")

ZIG_COMPILER_WRAPPER = """#!/bin/sh
case " $* " in
    *" -c "*|*" -S "*|*" -E "*) ;;
    *) for arg; do shift; case "$arg" in -std=*) ;; *) set -- "$@" "$arg" ;; esac; done ;;
esac
exec "{zigbuild}" zig {tool} -- -target {zig_target} {cpu} "$@"
"""

ZIG_TOOL_WRAPPER = """#!/bin/sh
exec zig {tool} "$@"
"""


def resolve_targets(names: list[str]) -> list[str]:
    return select_targets(names, list(TARGETS), "Linux")


def glibc_suffix(target: str) -> str:
    if target.startswith("riscv64gc-") and target.endswith("-gnu"):
        return f".{GLIBC_RISCV64}"
    if target.endswith(("-gnu", "-gnueabihf")):
        return f".{GLIBC}"
    return ""


def zig_target(target: str) -> str:
    return TARGETS[target]["zig"] + glibc_suffix(target)


def zig_wrapper_signature(target: str, zigbuild: str) -> str:
    return f'exec "{zigbuild}" zig $tool -- -target {zig_target(target)} {TARGETS[target]["cpu"]}\n'


def zig_wrappers(target: str, zigbuild: str) -> dict[str, str]:
    """File name -> content of the compiler wrappers CMake builds LLVM with, so LLVM
    uses the same zig front end, libc and libc++ that cargo-zigbuild links with."""
    compiler = {
        tool: ZIG_COMPILER_WRAPPER.format(zigbuild=zigbuild, tool=tool, zig_target=zig_target(target),
                                          cpu=TARGETS[target]["cpu"])
        for tool in ("cc", "c++")
    }
    return compiler | {tool: ZIG_TOOL_WRAPPER.format(tool=tool) for tool in ("ar", "ranlib")}


def cmake_args(target: str) -> list[str]:
    info = TARGETS[target]
    build = llvm_build_dir(PLATFORM, target)
    zig_dir = build / "zig"
    args = llvm_cmake_args(build, llvm_prefix(PLATFORM, target), target, "AArch64;ARM;X86;RISCV") + [
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


def cargo_command(target: str) -> list[str]:
    # cargo-zigbuild takes the glibc version as a suffix of the Rust target.
    return ["cargo", "zigbuild", "--lib", "--release", "--target", target + glibc_suffix(target)]


def find_archive(compiler_output: str, archive: str) -> str | None:
    return next((word for word in compiler_output.split() if word.endswith(f"/{archive}")), None)


def zigbuild_path(env: dict) -> str:
    path = os.environ.get("ZIGBUILD") or shutil.which("cargo-zigbuild", path=env["PATH"])
    if not path:
        raise BuildError("cargo-zigbuild not found (it comes from the pixi `cross` env)")
    return path


def write_zig_wrappers(target: str, build: Path, zigbuild: str) -> None:
    signature_file = build / "zig" / "target"
    signature = zig_wrapper_signature(target, zigbuild)
    if signature_file.is_file() and signature_file.read_text() != signature:
        log(f"zig target changed, removing old build dir {build}")
        shutil.rmtree(build)
    write_file(signature_file, signature)
    for name, text in zig_wrappers(target, zigbuild).items():
        write_file(build / "zig" / name, text, executable=True)


def copy_zig_libcxx(target: str, zigbuild: str, env: dict) -> None:
    dest = llvm_prefix(PLATFORM, target) / "libcxx"
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
    prefix = llvm_prefix(PLATFORM, target)
    write_llvm_config(prefix, LLVM_CFLAGS, LLVM_CXXFLAGS, "libLLVM*.a", LLVM_SYSTEM_LIBS)
    write_file(prefix / "lib" / "libffi.so", "INPUT()\n")
    if "-musl" in target:
        for lib in ("pthread", "dl", "m", "c++"):
            write_file(prefix / "lib" / f"lib{lib}.a", "!<arch>\n")
    copy_zig_libcxx(target, zigbuild, env)


def build_llvm(target: str, env: dict) -> None:
    zigbuild = zigbuild_path(env)
    if llvm_needs_build(llvm_prefix(PLATFORM, target), target):
        build = llvm_build_dir(PLATFORM, target)
        write_zig_wrappers(target, build, zigbuild)
        build_and_install_llvm(cmake_args(target), build, env)
    finish_llvm_install(target, zigbuild, env)


def build_library(target: str, env: dict) -> None:
    prefix = llvm_prefix(PLATFORM, target)
    require_llvm(prefix, f"python3 scripts/build_llvm_linux.py llvm {target}")
    log(f"building lib{ARTIFACT}.so for {target}")
    run(cargo_command(target), env={**env, "LLVM_SYS_140_PREFIX": str(prefix)}, cwd=ROOT)


def main(argv: list[str] | None = None) -> int:
    return platform_main(__file__, __doc__, argv, resolve_targets, build_llvm, build_library)


if __name__ == "__main__":
    sys.exit(main())
