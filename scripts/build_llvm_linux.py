#!/usr/bin/env python3
"""Linux / FreeBSD: cross-build LLVM 14 + lld with zig, then cross-compile the Rust library with cargo-zigbuild.

    python3 scripts/build_llvm_linux.py llvm [target ...]   # LLVM 14 + lld (slow, once per target)
    python3 scripts/build_llvm_linux.py lib  [target ...]   # cargo zigbuild of lib<artifact>.so
    python3 scripts/build_llvm_linux.py all  [target ...]   # both, in order (LLVM only if missing)

Targets: Rust target triples (default: all eleven below).

Every target, x86_64 included, links its own zig-built LLVM: the host LLVM needs
the host's new glibc/libstdc++ plus libz/libtinfo/libffi, which neither zig nor
Kotlin/Native's sysroots have. glibc targets are pinned to an old glibc (GLIBC,
default 2.17; riscv64's first is 2.27) so the libraries also link with
Kotlin/Native, whose Linux sysroots ship glibc 2.19 (x64) and 2.25 (arm64).
LLVM is installed into .toolchains/llvm14/linux/<target>. Output:
target/<rust target>/release/lib<artifact>.so (the justfile copies it into libkmp).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from install_toolchain import (
    ARTIFACT, ROOT, BuildError, build_and_install_llvm, build_env, ensure_cross_env,
    llvm_build_dir, llvm_cmake_args, llvm_installed, llvm_prefix, log, require_llvm,
    require_llvm_source, run, write_file, write_llvm_config,
)

GLIBC = os.environ.get("GLIBC", "2.17")
GLIBC_RISCV64 = os.environ.get("GLIBC_RISCV64", "2.27")

# Rust target -> zig target (without glibc version), the CPU baseline cargo-zigbuild
# uses for the Rust target, CMake system name / processor.
TARGETS = {
    "x86_64-unknown-linux-gnu":       {"zig": "x86_64-linux-gnu",      "cpu": "",                                       "system": "Linux",   "processor": "x86_64"},
    "aarch64-unknown-linux-gnu":      {"zig": "aarch64-linux-gnu",     "cpu": "",                                       "system": "Linux",   "processor": "aarch64"},
    "armv7-unknown-linux-gnueabihf":  {"zig": "arm-linux-gnueabihf",   "cpu": "-mcpu=generic+v7a+vfp3-d32+thumb2-neon", "system": "Linux",   "processor": "armv7"},
    "i686-unknown-linux-gnu":         {"zig": "x86-linux-gnu",         "cpu": "-mcpu=pentium4",                         "system": "Linux",   "processor": "i686"},
    "riscv64gc-unknown-linux-gnu":    {"zig": "riscv64-linux-gnu",     "cpu": "-mcpu=generic_rv64+m+a+f+d+c",           "system": "Linux",   "processor": "riscv64"},
    "x86_64-unknown-linux-musl":      {"zig": "x86_64-linux-musl",     "cpu": "",                                       "system": "Linux",   "processor": "x86_64"},
    "aarch64-unknown-linux-musl":     {"zig": "aarch64-linux-musl",    "cpu": "",                                       "system": "Linux",   "processor": "aarch64"},
    "armv7-unknown-linux-musleabihf": {"zig": "arm-linux-musleabihf",  "cpu": "-mcpu=generic+v7a+vfp3-d32+thumb2-neon", "system": "Linux",   "processor": "armv7"},
    "i686-unknown-linux-musl":        {"zig": "x86-linux-musl",        "cpu": "-mcpu=pentium4",                         "system": "Linux",   "processor": "i686"},
    "riscv64gc-unknown-linux-musl":   {"zig": "riscv64-linux-musl",    "cpu": "-mcpu=generic_rv64+m+a+f+d+c",           "system": "Linux",   "processor": "riscv64"},
    "x86_64-unknown-freebsd":         {"zig": "x86_64-freebsd",        "cpu": "",                                       "system": "FreeBSD", "processor": "x86_64"},
}

LLVM_CFLAGS = "-D_GNU_SOURCE -D__STDC_CONSTANT_MACROS -D__STDC_FORMAT_MACROS -D__STDC_LIMIT_MACROS"
LLVM_CXXFLAGS = f"-std=c++14 -fno-exceptions {LLVM_CFLAGS}"

# On 32-bit ARM, zig rejects -std=c++* on a link-only command ("not allowed with
# 'C'"), which fails CMake's compiler checks; the flag does nothing when
# linking, so the wrapper drops it there.
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
    if not names:
        return list(TARGETS)
    for name in names:
        if name not in TARGETS:
            raise BuildError(f"unknown Linux target: {name} (known: {', '.join(TARGETS)})")
    return names


def glibc_suffix(target: str) -> str:
    """'.<glibc version>' for glibc targets, '' for musl and FreeBSD."""
    if target.startswith("riscv64gc-") and target.endswith("-gnu"):
        return f".{GLIBC_RISCV64}"
    if target.endswith(("-gnu", "-gnueabihf")):
        return f".{GLIBC}"
    return ""


def zig_target(target: str) -> str:
    return TARGETS[target]["zig"] + glibc_suffix(target)


def zigbuild_path(env: dict) -> str:
    path = os.environ.get("ZIGBUILD") or shutil.which("cargo-zigbuild", path=env["PATH"])
    if not path:
        raise BuildError("cargo-zigbuild not found (it comes from the pixi `cross` env)")
    return path


# ------------------------------------------------------------------ LLVM


def write_zig_wrappers(target: str, build: Path, zigbuild: str) -> None:
    """Compiler wrappers so CMake builds LLVM with the same zig front end, libc and
    libc++ that cargo-zigbuild later links the library with."""
    zig_dir = build / "zig"
    cpu = TARGETS[target]["cpu"]
    # Ninja doesn't track the flags inside the wrappers, so a build made for
    # another zig target (e.g. another glibc) has to start over.
    signature = f'exec "{zigbuild}" zig $tool -- -target {zig_target(target)} {cpu}\n'
    signature_file = zig_dir / "target"
    if signature_file.is_file() and signature_file.read_text() != signature:
        log(f"zig target changed, removing old build dir {build}")
        shutil.rmtree(build)
    write_file(signature_file, signature)

    for tool in ("cc", "c++"):
        text = ZIG_COMPILER_WRAPPER.format(zigbuild=zigbuild, tool=tool, zig_target=zig_target(target), cpu=cpu)
        write_file(zig_dir / tool, text, executable=True)
    for tool in ("ar", "ranlib"):
        write_file(zig_dir / tool, ZIG_TOOL_WRAPPER.format(tool=tool), executable=True)


def cmake_args(target: str) -> list[str]:
    info = TARGETS[target]
    build = llvm_build_dir("linux", target)
    zig_dir = build / "zig"
    args = llvm_cmake_args(build, llvm_prefix("linux", target), target, "AArch64;ARM;X86;RISCV") + [
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
    # musl 1.2.4+ only declares lseek64 under _LARGEFILE64_SOURCE: CMake's C
    # check finds it, but the C++ sources can't see it. off_t is 64-bit on
    # musl, so plain lseek is enough.
    if "-musl" in target:
        args.append("-DHAVE_LSEEK64=0")
    return args


def copy_zig_libcxx(target: str, zigbuild: str, env: dict) -> None:
    """Consumers that don't link through zig (Kotlin/Native) need the libc++ LLVM
    was built against; zig builds it on demand for this target."""
    dest = llvm_prefix("linux", target) / "libcxx"
    libs = ("libc++.a", "libc++abi.a")
    if all((dest / lib).is_file() for lib in libs):
        return
    log(f"locating zig's libc++ for {target}")
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "cxx-probe.cpp"
        probe.write_text("int main(){}\n")
        cmd = [zigbuild, "zig", "c++", "--", "-target", zig_target(target), *TARGETS[target]["cpu"].split(),
               "-v", str(probe), "-o", str(Path(tmp) / "cxx-probe")]
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    words = (result.stdout + result.stderr).split()
    dest.mkdir(parents=True, exist_ok=True)
    for lib in libs:
        path = next((word for word in words if word.endswith(f"/{lib}")), None)
        if path is None:
            raise BuildError(f"could not locate zig's {lib} for {target}:\n{result.stderr[-2000:]}")
        shutil.copy2(path, dest / lib)


def finish_llvm_install(target: str, zigbuild: str, env: dict) -> None:
    """Make the install usable from the host: llvm-config stand-in, link stubs, libc++."""
    prefix = llvm_prefix("linux", target)
    write_llvm_config(prefix, LLVM_CFLAGS, LLVM_CXXFLAGS, "libLLVM*.a", "-lpthread -ldl -lm")
    # lld_rs links `ffi` dynamically on every non-Windows target, and zig's
    # linker only accepts a .so for that. LLVM is built without libffi, so an
    # empty linker script satisfies it without adding a DT_NEEDED entry.
    # (`stdc++`/`c++` from llvm-sys resolve to zig's libc++, which LLVM was built against.)
    write_file(prefix / "lib" / "libffi.so", "INPUT()\n")
    # On musl, llvm-sys links the system libraries statically, so rustc wants
    # libpthread.a, libdl.a, libm.a and libc++.a. The first three are part of
    # musl's libc, and zig's libc++ already comes in through lld_rs's -lstdc++,
    # so empty archives satisfy it without a second libc++.
    if "-musl" in target:
        for lib in ("pthread", "dl", "m", "c++"):
            write_file(prefix / "lib" / f"lib{lib}.a", "!<arch>\n")
    copy_zig_libcxx(target, zigbuild, env)


def build_llvm(target: str, env: dict) -> None:
    prefix = llvm_prefix("linux", target)
    zigbuild = zigbuild_path(env)
    if llvm_installed(prefix):
        log(f"LLVM 14 for {target} already installed: {prefix}")
    else:
        require_llvm_source()
        log(f"building LLVM 14 + lld for {target} with zig (slow, once per target)")
        build = llvm_build_dir("linux", target)
        write_zig_wrappers(target, build, zigbuild)
        build_and_install_llvm(cmake_args(target), build, env)
    finish_llvm_install(target, zigbuild, env)


# ------------------------------------------------------------------ Rust library


def cargo_command(target: str) -> list[str]:
    # cargo-zigbuild takes the glibc version as a suffix of the Rust target.
    return ["cargo", "zigbuild", "--lib", "--release", "--target", target + glibc_suffix(target)]


def build_library(target: str, env: dict) -> None:
    prefix = llvm_prefix("linux", target)
    require_llvm(prefix, f"python3 scripts/build_llvm_linux.py llvm {target}")
    log(f"building lib{ARTIFACT}.so for {target}")
    run(cargo_command(target), env={**env, "LLVM_SYS_140_PREFIX": str(prefix)}, cwd=ROOT)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage", choices=["llvm", "lib", "all"])
    parser.add_argument("targets", nargs="*", metavar="target")
    args = parser.parse_args(argv)
    ensure_cross_env(__file__, argv)

    try:
        env = build_env()
        targets = resolve_targets(args.targets)
        if args.stage in ("llvm", "all"):
            for target in targets:
                build_llvm(target, env)
        if args.stage in ("lib", "all"):
            for target in targets:
                build_library(target, env)
    except BuildError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
