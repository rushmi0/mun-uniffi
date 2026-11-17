#!/usr/bin/env python3
"""Windows (MSVC): cross-build LLVM 14 + lld with clang-cl + xwin, then cross-compile the Rust DLL.

    python3 scripts/build_llvm_windows.py llvm [target ...]   # LLVM 14 + lld (slow, once per target)
    python3 scripts/build_llvm_windows.py lib  [target ...]   # cargo build of <artifact>.dll
    python3 scripts/build_llvm_windows.py all  [target ...]   # both, in order (LLVM only if missing)

Targets: x86_64-pc-windows-msvc i686-pc-windows-msvc aarch64-pc-windows-msvc (default: all).
The MSVC CRT + Windows SDK come from cargo-xwin (XWIN_ARCH must include x86;
cargo-xwin's default is only x86_64,aarch64). LLVM is installed into
.toolchains/llvm14/windows/<target>. Output: target/<rust target>/release/<artifact>.dll
(the justfile copies it into libkmp).
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import sys
from pathlib import Path

from install_toolchain import (
    ARTIFACT, ROOT, TOOLCHAINS, BuildError, build_and_install_llvm, build_env, capture,
    ensure_cross_env, llvm_build_dir, llvm_cmake_args, llvm_installed, llvm_prefix, log,
    require_llvm, require_llvm_source, run, write_file, write_llvm_config,
)

TARGETS = ["x86_64-pc-windows-msvc", "i686-pc-windows-msvc", "aarch64-pc-windows-msvc"]

XWIN_ARCH = os.environ.get("XWIN_ARCH", "x86,x86_64,aarch64")
XWIN_CACHE = Path(os.environ.get("XWIN_CACHE", Path.home() / ".cache" / "cargo-xwin"))
SHIMS_DIR = TOOLCHAINS / "windows-shims"

LLVM_CFLAGS = (
    "-D_CRT_SECURE_NO_DEPRECATE -D_CRT_SECURE_NO_WARNINGS "
    "-D__STDC_CONSTANT_MACROS -D__STDC_FORMAT_MACROS -D__STDC_LIMIT_MACROS"
)
LLVM_SYSTEM_LIBS = "psapi.lib shell32.lib ole32.lib uuid.lib advapi32.lib"

# Stand-in for MASM's ml.exe, found through PATH. Only libffi-sys calls it: cc
# hands it libffi's preprocessed sysv_intel.asm for i686-pc-windows-msvc.
# llvm-ml can't parse that file (`PROC C`, `::` labels), so this assembles
# libffi's GNU-syntax sysv.S with clang-cl instead, which is what libffi-sys
# itself does for x86_64. sysv.S is wrapped in `#ifndef _MSC_VER`, hence
# -U_MSC_VER; it emits the same decorated names (@ffi_call_i386@8,
# _ffi_closure_i386, ...) the MSVC-built C code expects.
ML_SHIM = r"""#!/bin/sh
set -eu

src="${CARGO_MANIFEST_DIR:-}"
case "$(basename "$src")" in
    libffi-sys-*) ;;
    *) echo "ml.exe stand-in: only libffi-sys is supported (CARGO_MANIFEST_DIR=$src)" >&2; exit 1 ;;
esac
out=
for arg; do case "$arg" in -Fo*) out="${arg#-Fo}" ;; esac; done
[ -n "$out" ] || { echo "ml.exe stand-in: no -Fo<object> argument" >&2; exit 1; }

cd "$src"
exec "${CLANG_CL:-clang-cl}" --target=i686-pc-windows-msvc -U_MSC_VER \
    /Ilibffi /Ilibffi/include /Ilibffi/src/x86 /Iinclude/msvc/x86 /DFFI_STATIC_BUILD \
    /c libffi/src/x86/sysv.S "/Fo$out"
"""

# Stand-in for armasm64.exe, found through PATH. Only libffi-sys calls it: cc
# hands it libffi's preprocessed win64_armasm.asm for aarch64-pc-windows-msvc,
# and LLVM has no armasm-compatible assembler. This assembles libffi's
# GNU-syntax sysv.S with clang-cl instead. With _MSC_VER defined, ffi.c also
# expects extend_hfa_type and compress_hfa_type from the assembly, which only
# win64_armasm.S has, so those two are converted from it to GNU syntax (the
# instructions are the same; only LEAF_ENTRY/LEAF_END, ALIGN and labels differ).
ARMASM64_SHIM = r"""#!/bin/sh
set -eu

src="${CARGO_MANIFEST_DIR:-}"
case "$(basename "$src")" in
    libffi-sys-*) ;;
    *) echo "armasm64.exe stand-in: only libffi-sys is supported (CARGO_MANIFEST_DIR=$src)" >&2; exit 1 ;;
esac
out=
for arg; do case "$arg" in -Fo*) out="${arg#-Fo}" ;; esac; done
[ -n "$out" ] || { echo "armasm64.exe stand-in: no -Fo<object> argument" >&2; exit 1; }

cd "$src"
asm="$out.S"
{
    echo '#include "sysv.S"'
    echo '	.text'
    sed -n '/^[[:space:]]*LEAF_ENTRY[[:space:]]*extend_hfa_type/,/^[[:space:]]*LEAF_END[[:space:]]*compress_hfa_type/p' \
        libffi/src/aarch64/win64_armasm.S |
    sed -E \
        -e 's/^[[:space:]]*LEAF_ENTRY[[:space:]]+([A-Za-z_][A-Za-z0-9_]*).*/	.globl \1\n	.p2align 2\n\1:/' \
        -e '/^[[:space:]]*LEAF_END/d' \
        -e 's/^[[:space:]]*ALIGN[[:space:]]+4[[:space:]]*$/	.p2align 2/' \
        -e 's/^([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*$/\1:/'
} > "$asm"
grep -q '^compress_hfa_type:' "$asm" || { echo "armasm64.exe stand-in: HFA helpers not found in win64_armasm.S" >&2; exit 1; }

exec "${CLANG_CL:-clang-cl}" --target=aarch64-pc-windows-msvc -U_MSC_VER \
    /Ilibffi /Ilibffi/include /Ilibffi/src/aarch64 /Iinclude/msvc/aarch64 /DFFI_STATIC_BUILD \
    /c "$asm" "/Fo$out"
"""


def resolve_targets(names: list[str]) -> list[str]:
    if not names:
        return list(TARGETS)
    for name in names:
        if name not in TARGETS:
            raise BuildError(f"unknown Windows target: {name} (known: {', '.join(TARGETS)})")
    return names


def xwin_base_env(env: dict) -> dict:
    """Environment shared by the LLVM and library builds."""
    return {**env, "XWIN_ARCH": XWIN_ARCH}


def remove_stale_xwin_clang_link() -> None:
    """cargo-xwin links clang-cl into its cache; if the clang it pointed to is gone
    (e.g. a recreated pixi env) the dangling link breaks every later call."""
    link = XWIN_CACHE / "clang-cl"
    if link.is_symlink() and not link.exists():
        link.unlink()


# ------------------------------------------------------------------ LLVM


def cmake_args(target: str) -> list[str]:
    # LLVM_BUILD_LLVM_C_DYLIB defaults to ON with MSVC; LLVM-C.dll needs a host
    # llvm-nm, which would start a NATIVE sub-build with clang-cl as the host
    # compiler. llvm-sys links the static libraries, so it isn't needed.
    toolchain = XWIN_CACHE / "cmake" / "clang-cl" / f"{target}-toolchain.cmake"
    build = llvm_build_dir("windows", target)
    ninja = shutil.which("ninja") or "ninja"
    return llvm_cmake_args(build, llvm_prefix("windows", target), target, "AArch64;ARM;X86") + [
        f"-DCMAKE_MAKE_PROGRAM={ninja}",
        f"-DCMAKE_TOOLCHAIN_FILE={toolchain}",
        "-DCMAKE_RC_COMPILER=llvm-rc",
        "-DCMAKE_MT=llvm-mt",
        "-DLLVM_USE_CRT_RELEASE=MD",
        "-DLLVM_BUILD_LLVM_C_DYLIB=OFF",
        "-DLLVM_ENABLE_DIA_SDK=OFF",
    ]


def finish_llvm_install(target: str) -> None:
    """Make the install usable from the host: llvm-config stand-in and link stubs.

    On MSVC, llvm-sys and lld_rs expect `.lib` file names from --libnames and
    --system-libs. inkwell and lld_rs check the host OS, not the target, so from
    Linux they ask for ffi.lib; LLVM is built without libffi and libffi-sys links
    its own, so an empty archive satisfies them.
    """
    prefix = llvm_prefix("windows", target)
    write_llvm_config(prefix, LLVM_CFLAGS, LLVM_CFLAGS, "LLVM*.lib", LLVM_SYSTEM_LIBS)
    write_file(prefix / "lib" / "ffi.lib", "!<arch>\n")


def build_llvm(target: str, env: dict) -> None:
    prefix = llvm_prefix("windows", target)
    if llvm_installed(prefix):
        log(f"LLVM 14 for {target} already installed: {prefix}")
    else:
        require_llvm_source()
        log(f"building LLVM 14 + lld for {target} with clang-cl (slow, once per target)")
        llvm_env = xwin_base_env(env)
        remove_stale_xwin_clang_link()
        # Downloads the MSVC CRT + Windows SDK if needed and writes the clang-cl
        # toolchain file for this target; the printed variables aren't needed.
        capture(["cargo", "xwin", "env", "--target", target], env=llvm_env)
        build_and_install_llvm(cmake_args(target), llvm_build_dir("windows", target), llvm_env)
    finish_llvm_install(target)


# ------------------------------------------------------------------ Rust library


def parse_xwin_env(output: str) -> dict[str, str]:
    """Parse `cargo xwin env` output: lines like  export NAME="value";"""
    parsed = {}
    for line in output.splitlines():
        words = shlex.split(line.strip().rstrip(";"))
        if len(words) == 2 and words[0] == "export" and "=" in words[1]:
            name, value = words[1].split("=", 1)
            parsed[name] = value
    return parsed


def write_shims() -> None:
    write_file(SHIMS_DIR / "ml.exe", ML_SHIM, executable=True)
    write_file(SHIMS_DIR / "armasm64.exe", ARMASM64_SHIM, executable=True)


def cargo_env(target: str, env: dict) -> dict:
    """cargo-xwin's environment plus the LLVM flags.

    Plain cargo with cargo-xwin's environment instead of `cargo xwin build`: its
    CFLAGS_<target>/CXXFLAGS_<target> hide the LLVM include flags llvm-sys and
    lld_rs pass to cc through CFLAGS/CXXFLAGS.
    """
    prefix = llvm_prefix("windows", target)
    env = xwin_base_env(env)
    env["PATH"] = f"{SHIMS_DIR}{os.pathsep}{env['PATH']}"  # ml.exe / armasm64.exe stand-ins
    env["CLANG_CL"] = "clang-cl"
    real_clang = shutil.which("clang", path="/usr/local/bin:/usr/bin:/bin")
    if real_clang:
        env["LUNA_REAL_CLANG"] = real_clang

    # `cargo xwin env` prints a PATH derived from the PATH it ran with, so it
    # must run with the shims already on PATH, or they are lost.
    remove_stale_xwin_clang_link()
    env.update(parse_xwin_env(capture(["cargo", "xwin", "env", "--target", target], env=env)))

    llvm_config = prefix / "bin" / "llvm-config"
    key = target.replace("-", "_")
    # libffi's dlmalloc.c passes int* to the Interlocked* functions, which MSVC
    # accepts but newer clang rejects by default.
    env[f"CFLAGS_{key}"] = (
        f"{env.get(f'CFLAGS_{key}', '')} {capture([llvm_config, '--cflags'])} "
        "-Wno-error=incompatible-pointer-types"
    )
    env[f"CXXFLAGS_{key}"] = f"{env.get(f'CXXFLAGS_{key}', '')} {capture([llvm_config, '--cxxflags'])}"
    env["LLVM_SYS_140_PREFIX"] = str(prefix)
    return env


def build_library(target: str, env: dict) -> None:
    require_llvm(llvm_prefix("windows", target), f"python3 scripts/build_llvm_windows.py llvm {target}")
    write_shims()
    log(f"building {ARTIFACT}.dll for {target}")
    run(["cargo", "build", "--lib", "--release", "--target", target], env=cargo_env(target, env), cwd=ROOT)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage", choices=["llvm", "lib", "all"])
    parser.add_argument("targets", nargs="*", metavar="target")
    args = parser.parse_args(argv)
    ensure_cross_env(__file__, argv)

    try:
        targets = resolve_targets(args.targets)
        env = build_env()
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
