#!/usr/bin/env python3
"""Toolchain and cross-build workflow for a Linux x86_64 host (called by the justfile).

    python3 scripts/run_workflow.py setup                  # check / install the toolchain
    python3 scripts/run_workflow.py llvm  [name ...]       # LLVM 14 + lld, only where missing
    python3 scripts/run_workflow.py build [name ...]       # LLVM if missing, then the Rust libraries
    python3 scripts/run_workflow.py build android --dry-run   # list the stages only

A name is a platform (android, linux, windows), a Rust target triple or an
Android ABI; no names means all three platforms. Each platform runs as its own
build_llvm_<platform>.py process. Stages run in order and the first failure
stops the rest, so a failed run can simply be repeated: finished LLVM installs
are skipped and cargo builds are incremental.

Bindings, artifact copies and the Gradle build are justfile recipes.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import build_llvm_android
import build_llvm_linux
import build_llvm_windows
from install_toolchain import ROOT, BuildError, ensure_cross_env, log, run

SCRIPTS_DIR = Path(__file__).resolve().parent

# Platform -> (script, the names that script accepts)
PLATFORMS = {
    "android": ("build_llvm_android.py",
                [*build_llvm_android.ABIS, *(rust for rust, _ in build_llvm_android.ABIS.values())]),
    "linux": ("build_llvm_linux.py", list(build_llvm_linux.TARGETS)),
    "windows": ("build_llvm_windows.py", list(build_llvm_windows.TARGETS)),
}


def group_by_platform(names: list[str]) -> dict[str, list[str]]:
    """{platform: names for its script}; an empty list means all of its targets."""
    if not names:
        return {platform: [] for platform in PLATFORMS}
    groups: dict[str, list[str]] = {}
    for name in names:
        if name in PLATFORMS:
            groups[name] = []
            continue
        platform = next((p for p, (_, known) in PLATFORMS.items() if name in known), None)
        if platform is None:
            raise BuildError(f"unknown platform or target: {name}")
        if groups.get(platform) != []:  # a whole-platform request already covers it
            groups.setdefault(platform, []).append(name)
    return groups


def plan(command: str, names: list[str]) -> list[tuple[str, list[str]]]:
    """Stages as (description, script command line)."""
    if command == "setup":
        return [("toolchain", ["install_toolchain.py"])]
    steps = ["llvm"] if command == "llvm" else ["llvm", "lib"]
    stage_names = {"llvm": "LLVM 14", "lib": "Rust library"}
    stages = []
    for platform, targets in group_by_platform(names).items():
        script, _ = PLATFORMS[platform]
        for step in steps:
            stages.append((f"{platform} {stage_names[step]}", [script, step, *targets]))
    return stages


def run_stages(stages: list[tuple[str, list[str]]]) -> int:
    """Run stages in order, stop at the first failure, print a summary."""
    finished = []
    for index, (name, command) in enumerate(stages, start=1):
        log(f"[{index}/{len(stages)}] {name}")
        started = time.monotonic()
        try:
            run([sys.executable, SCRIPTS_DIR / command[0], *command[1:]], cwd=ROOT)
        except BuildError as error:
            print(f"\nerror: stage '{name}' failed: {error}", file=sys.stderr)
            not_run = [later for later, _ in stages[index:]]
            if not_run:
                print(f"not run: {', '.join(not_run)}", file=sys.stderr)
            return 1
        finished.append((name, time.monotonic() - started))

    for name, seconds in finished:
        print(f"  {seconds:8.1f}s  {name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["setup", "llvm", "build"])
    parser.add_argument("names", nargs="*", metavar="name")
    parser.add_argument("--dry-run", action="store_true", help="list the stages without running them")
    args = parser.parse_args(argv)

    try:
        stages = plan(args.command, args.names)
    except BuildError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.dry_run:
        for index, (name, command) in enumerate(stages, start=1):
            print(f"{index:2}. {name:24} {' '.join(command)}")
        return 0

    ensure_cross_env(__file__, argv)
    try:
        return run_stages(stages)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
