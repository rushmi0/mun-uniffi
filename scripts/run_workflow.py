#!/usr/bin/env python3

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence
from typing import NamedTuple

import build_llvm_android
import build_llvm_linux
import build_llvm_windows
from toolchain import ROOT, SCRIPTS_DIR, BuildError, ensure_cross_env, fail, log, run


class Platform(NamedTuple):
    script: str                # build script that handles this platform
    targets: list[str]         # every target name the platform accepts


class Stage(NamedTuple):
    name: str                  # label shown in progress and timing output
    command: list[str]         # script name followed by its arguments


PLATFORMS = {
    "android": Platform(
        "build_llvm_android.py",
        [*build_llvm_android.ABIS, *(rust for rust, _ in build_llvm_android.ABIS.values())],
    ),
    "linux": Platform("build_llvm_linux.py", list(build_llvm_linux.TARGETS)),
    "windows": Platform("build_llvm_windows.py", build_llvm_windows.TARGETS),
}
STAGE_NAMES = {"llvm": "LLVM 14", "lib": "Rust library"}


def platform_of(name: str) -> str:
    for platform, known in PLATFORMS.items():
        if name in known.targets:
            return platform
    raise BuildError(f"unknown platform or target: {name}")


def group_by_platform(names: Sequence[str]) -> dict[str, list[str]]:
    """Map each platform to its requested targets, in first-mentioned order.

    An empty list means every target of that platform.
    """
    if not names:
        return {platform: [] for platform in PLATFORMS}
    groups: dict[str, list[str]] = {}
    for name in names:
        if name in PLATFORMS:
            groups[name] = []          # the whole platform was requested
            continue
        platform = platform_of(name)
        if groups.get(platform) == []:  # the whole platform is already selected
            continue
        groups.setdefault(platform, []).append(name)
    return groups


def plan(command: str, names: Sequence[str]) -> list[Stage]:
    """The ordered build stages for `command` over `names`."""
    if command == "setup":
        return [Stage("toolchain", ["install_toolchain.py"])]
    steps = ["llvm"] if command == "llvm" else ["llvm", "lib"]
    return [
        Stage(f"{platform} {STAGE_NAMES[step]}", [PLATFORMS[platform].script, step, *targets])
        for platform, targets in group_by_platform(names).items()
        for step in steps
    ]


def run_stages(stages: list[Stage]) -> int:
    """Run stages in order, stopping at the first failure. Prints per-stage timings on success."""
    timings = []
    for index, (name, command) in enumerate(stages, start=1):
        log(f"[{index}/{len(stages)}] {name}")
        started = time.monotonic()
        try:
            run([sys.executable, SCRIPTS_DIR / command[0], *command[1:]], cwd=ROOT)
        except BuildError as error:
            fail(BuildError(f"stage '{name}' failed: {error}"))
            if not_run := [later.name for later in stages[index:]]:
                print(f"not run: {', '.join(not_run)}", file=sys.stderr)
            return 1
        timings.append((name, time.monotonic() - started))

    for name, seconds in timings:
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
        fail(error)
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
