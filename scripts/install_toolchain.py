#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import tomllib
import urllib.request
import zipfile
from collections.abc import Callable
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLCHAINS = Path(os.environ.get("TOOLCHAINS_DIR", ROOT / ".toolchains"))
DOWNLOADS = TOOLCHAINS / "downloads"
TOOLS_DIR = ROOT / ".pixi" / "tools"
TOOLS_BIN = TOOLS_DIR / "bin"
KMP_DIR = ROOT / "libkmp"
ARTIFACT = os.environ.get("ARTIFACT", "mun")
JOBS = os.environ.get("JOBS", str(os.cpu_count() or 4))

LLVM_VERSION = "14.0.6"
LLVM_URL = (
    "https://github.com/llvm/llvm-project/releases/download/"
    f"llvmorg-{LLVM_VERSION}/llvm-project-{LLVM_VERSION}.src.tar.xz"
)
LLVM_SHA256 = "8b3cfd7bc695bd6cea0f37f53f0981f34f87496e79e2529874fd03a2f9dd3a8a"
LLVM_SRC = Path(os.environ.get("LLVM_SRC", TOOLCHAINS / "src" / f"llvm-project-{LLVM_VERSION}.src"))
LLVM_INSTALL_ROOT = Path(os.environ.get("LLVM14_ROOT", TOOLCHAINS / "llvm14"))
LLVM_BUILD_ROOT = Path(os.environ.get("LLVM14_BUILD", TOOLCHAINS / "build"))
# Host LLVM 14 from the pixi `default` env: its llvm-tblgen drives the cross builds.
HOST_LLVM = Path(os.environ.get("LLVM14_HOST", ROOT / ".pixi" / "envs" / "default"))

NDK_URL = "https://dl.google.com/android/repository/android-ndk-r30-linux.zip"
NDK_SHA256 = "753611f410d002cfcd3f3dc2ef49aad532089d3180b436c060a90bf0fcb64df2"
MANAGED_NDK = TOOLCHAINS / "android-ndk-r30"
ANDROID_API = os.environ.get("ANDROID_API", "24")

SDK_TOOLS_URL = "https://dl.google.com/android/repository/commandlinetools-linux-13114758_latest.zip"
SDK_TOOLS_SHA256 = "7ec965280a073311c339e571cd5de778b9975026cfcbe79f2b1cdcb1e15317ee"
MANAGED_SDK = Path(os.environ.get("SDK_DIR", TOOLCHAINS / "android-sdk"))
SDK_BUILD_TOOLS = "36.0.0"

# gobley-uniffi-bindgen is installed by the justfile, which needs it on every OS.
CARGO_TOOLS = {"cargo-xwin": "0.23.1"}

REQUIRED_COMMANDS = [
    "cargo", "rustup", "cmake", "ninja", "zig", "cargo-zigbuild",
    "clang-cl", "lld-link", "llvm-lib", "llvm-rc", "llvm-mt", "java",
]

# The installed llvm-config is a target binary, so llvm-sys and lld_rs get a
# host-side stand-in that describes the installation it lives in.
LLVM_CONFIG_STANDIN = r"""#!/usr/bin/env bash
prefix="$(cd "$(dirname "$0")/.." && pwd)"
for arg in "$@"; do
    case "$arg" in
        --version)    echo "{version}" ;;
        --prefix)     echo "$prefix" ;;
        --libdir)     echo "$prefix/lib" ;;
        --includedir) echo "$prefix/include" ;;
        --cflags)     echo "-I$prefix/include {cflags}" ;;
        --cxxflags)   echo "-I$prefix/include {cxxflags}" ;;
        --libnames)   (cd "$prefix/lib" && ls {libnames} | tr '\n' ' '; echo) ;;
        --system-libs) echo "{system_libs}" ;;
        --shared-mode) echo "static" ;;
        --build-mode) echo "Release" ;;
        --link-static|--link-shared|core) ;;
        *) echo "llvm-config stand-in: unsupported argument $arg" >&2; exit 1 ;;
    esac
done
"""

class BuildError(Exception):
    pass


def log(message: str) -> None:
    print(f"==> {message}", flush=True)


def fail(error: BuildError) -> int:
    print(f"error: {error}", file=sys.stderr)
    return 1


def run(cmd: list, env: dict | None = None, cwd: Path | None = None) -> None:
    cmd = [str(part) for part in cmd]
    print(f"+ {shlex.join(cmd)}", flush=True)
    try:
        result = subprocess.run(cmd, env=env, cwd=cwd)
    except FileNotFoundError:
        raise BuildError(f"command not found: {cmd[0]}") from None
    if result.returncode != 0:
        raise BuildError(f"{Path(cmd[0]).name} exited with code {result.returncode} (command shown above)")


def capture(cmd: list, env: dict | None = None, cwd: Path | None = None) -> str:
    cmd = [str(part) for part in cmd]
    try:
        result = subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True)
    except FileNotFoundError:
        raise BuildError(f"command not found: {cmd[0]}") from None
    if result.returncode != 0:
        raise BuildError(f"exit code {result.returncode}: {shlex.join(cmd)}\n{result.stderr.strip()}")
    return result.stdout.strip()


def write_file(path: Path, text: str, executable: bool = False) -> None:
    """Write only when the content differs, so unchanged files keep their mtime."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file() or path.read_text() != text:
        path.write_text(text)
    if executable:
        path.chmod(0o755)


def build_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = f"{TOOLS_BIN}{os.pathsep}{env.get('PATH', '')}"
    return env


def require_host_linux() -> None:
    system, machine = platform.system(), platform.machine()
    if system != "Linux" or machine not in ("x86_64", "AMD64"):
        raise BuildError(f"the cross builds need a Linux x86_64 host (this is {system} {machine})")


def ensure_cross_env(script: str, argv: list[str]) -> None:
    """Re-run `script` inside the pixi `cross` env unless already there (or pixi is absent)."""
    try:
        require_host_linux()
    except BuildError as error:
        sys.exit(fail(error))
    if os.environ.get("PIXI_ENVIRONMENT_NAME") == "cross":
        return
    pixi = shutil.which("pixi")
    manifest = ROOT / "pixi.toml"
    if pixi is None or not manifest.is_file():
        log("pixi not found: using the tools on PATH instead of the pixi `cross` env")
        return
    log("entering the pixi `cross` environment")
    cmd = [pixi, "run", "--manifest-path", str(manifest), "-e", "cross",
           sys.executable, str(Path(script).resolve()), *argv]
    sys.exit(subprocess.run(cmd).returncode)


def select_targets(names: list[str], known: list[str], kind: str) -> list[str]:
    for name in names:
        if name not in known:
            raise BuildError(f"unknown {kind} target: {name} (known: {', '.join(known)})")
    return names or list(known)


def llvm_prefix(platform_name: str, name: str) -> Path:
    return LLVM_INSTALL_ROOT / platform_name / name


def llvm_build_dir(platform_name: str, name: str) -> Path:
    return LLVM_BUILD_ROOT / platform_name / name


def llvm_installed(prefix: Path) -> bool:
    # The llvm-config stand-in is written last, so it marks a finished install.
    return (prefix / "bin" / "llvm-config").is_file()


def require_llvm(prefix: Path, hint: str) -> None:
    if not llvm_installed(prefix):
        raise BuildError(f"no LLVM 14 in {prefix}; run: {hint}")


def llvm_needs_build(prefix: Path, label: str) -> bool:
    if llvm_installed(prefix):
        log(f"LLVM 14 for {label} already installed: {prefix}")
        return False
    if not (LLVM_SRC / ".patched").is_file():
        raise BuildError(f"LLVM source not ready in {LLVM_SRC}; run: python3 scripts/install_toolchain.py")
    log(f"building LLVM 14 + lld for {label} (slow, once per target)")
    return True


def llvm_cmake_args(build: Path, prefix: Path, triple: str, targets_to_build: str) -> list[str]:
    return [
        "-G", "Ninja",
        "-S", str(LLVM_SRC / "llvm"),
        "-B", str(build),
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DCMAKE_INSTALL_PREFIX={prefix}",
        "-DLLVM_ENABLE_PROJECTS=lld",
        f"-DLLVM_TARGETS_TO_BUILD={targets_to_build}",
        f"-DLLVM_HOST_TRIPLE={triple}",
        f"-DLLVM_DEFAULT_TARGET_TRIPLE={triple}",
        f"-DLLVM_TABLEGEN={HOST_LLVM / 'bin' / 'llvm-tblgen'}",
        f"-DLLVM_CONFIG_PATH={HOST_LLVM / 'bin' / 'llvm-config'}",
        "-DLLVM_BUILD_TOOLS=OFF",
        "-DLLD_BUILD_TOOLS=ON",
        "-DLLVM_BUILD_UTILS=OFF",
        "-DLLVM_INCLUDE_TESTS=OFF",
        "-DLLVM_INCLUDE_EXAMPLES=OFF",
        "-DLLVM_INCLUDE_BENCHMARKS=OFF",
        "-DLLVM_INCLUDE_DOCS=OFF",
        "-DLLVM_ENABLE_BINDINGS=OFF",
        "-DLLVM_ENABLE_PLUGINS=OFF",
        "-DLLVM_ENABLE_ZLIB=OFF",
        "-DLLVM_ENABLE_TERMINFO=OFF",
        "-DLLVM_ENABLE_LIBXML2=OFF",
        "-DLLVM_ENABLE_LIBEDIT=OFF",
        "-DLLVM_ENABLE_FFI=OFF",
    ]


def build_and_install_llvm(cmake_args: list[str], build: Path, env: dict) -> None:
    run(["cmake", *cmake_args], env=env)
    run(["ninja", "-C", build, "-j", JOBS, "install"], env=env)


def write_llvm_config(prefix: Path, cflags: str, cxxflags: str, libnames: str, system_libs: str) -> None:
    text = LLVM_CONFIG_STANDIN.format(
        version=LLVM_VERSION, cflags=cflags, cxxflags=cxxflags, libnames=libnames, system_libs=system_libs,
    )
    write_file(prefix / "bin" / "llvm-config", text, executable=True)



def platform_main(
    script: str,
    description: str,
    argv: list[str] | None,
    resolve: Callable[[list[str]], list[str]],
    build_llvm: Callable[[str, dict], None],
    build_library: Callable[[str, dict], None],
) -> int:
    """Shared `llvm | lib | all [target ...]` entry point of the build_llvm_*.py scripts."""
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description=description, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage", choices=["llvm", "lib", "all"])
    parser.add_argument("targets", nargs="*", metavar="target")
    args = parser.parse_args(argv)
    ensure_cross_env(script, argv)

    try:
        targets = resolve(args.targets)
        env = build_env()
        if args.stage in ("llvm", "all"):
            for target in targets:
                build_llvm(target, env)
        if args.stage in ("lib", "all"):
            for target in targets:
                build_library(target, env)
    except BuildError as error:
        return fail(error)
    return 0


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, sha256: str, attempts: int = 3) -> Path:
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    dest = DOWNLOADS / url.rsplit("/", 1)[-1]
    if dest.is_file():
        if sha256_of(dest) == sha256:
            log(f"using cached {dest.name}")
            return dest
        log(f"checksum mismatch, downloading {dest.name} again")
        dest.unlink()

    part_file = dest.with_name(dest.name + ".part")
    for attempt in range(1, attempts + 1):
        log(f"downloading {url} (attempt {attempt}/{attempts})")
        try:
            with urllib.request.urlopen(url, timeout=60) as response, part_file.open("wb") as out:
                shutil.copyfileobj(response, out, 1 << 20)
            break
        except OSError as error:
            part_file.unlink(missing_ok=True)
            if attempt == attempts:
                raise BuildError(f"could not download {url}: {error}") from None
            log(f"download failed ({error}), retrying")
            time.sleep(2 * attempt)

    actual = sha256_of(part_file)
    if actual != sha256:
        part_file.unlink()
        raise BuildError(f"checksum mismatch for {url}: expected {sha256}, got {actual}")
    part_file.replace(dest)
    return dest


def extract_zip(archive: Path, dest: Path) -> None:
    # zipfile.extractall drops Unix permissions and symlinks, which leaves the
    # NDK's compilers and the SDK's sdkmanager unusable.
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            if info.filename.startswith("/") or ".." in Path(info.filename).parts:
                raise BuildError(f"unsafe path {info.filename!r} in {archive}")
            mode = info.external_attr >> 16
            target = dest / info.filename
            if stat.S_ISLNK(mode):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.unlink(missing_ok=True)
                target.symlink_to(zf.read(info).decode())
                continue
            zf.extract(info, dest)
            if mode and not info.is_dir():
                target.chmod(stat.S_IMODE(mode))


def extract_archive(archive: Path, dest: Path) -> None:
    log(f"extracting {archive.name}")
    dest.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".zip"):
        extract_zip(archive, dest)
    elif archive.name.endswith((".tar.xz", ".tar.gz", ".tgz")):
        with tarfile.open(archive) as tf:
            tf.extractall(dest, filter="data")
    else:
        raise BuildError(f"unsupported archive format: {archive}")


def unpack_top_dir(archive: Path, dest: Path, top_dir_glob: str) -> None:
    """Extract into a staging dir, then move the archive's top-level dir to `dest`,
    so an interrupted run never leaves a half-extracted toolchain behind."""
    staging = dest.parent / f".{dest.name}.partial"
    if staging.exists():
        shutil.rmtree(staging)
    extract_archive(archive, staging)
    matches = list(staging.glob(top_dir_glob))
    if len(matches) != 1:
        raise BuildError(f"expected one {top_dir_glob} directory in {archive}, found {len(matches)}")
    matches[0].rename(dest)
    shutil.rmtree(staging)


def android_ndk_dir() -> Path:
    if "NDK_DIR" in os.environ:
        return Path(os.environ["NDK_DIR"])
    home = os.environ.get("ANDROID_NDK_HOME")
    if not (MANAGED_NDK / "source.properties").is_file() and home and (Path(home) / "source.properties").is_file():
        return Path(home)
    return MANAGED_NDK


def android_sdk_dir() -> Path:
    home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if not (MANAGED_SDK / "platforms").is_dir() and home and (Path(home) / "platforms").is_dir():
        return Path(home)
    return MANAGED_SDK


def ndk_bin_dir() -> Path:
    return android_ndk_dir() / "toolchains" / "llvm" / "prebuilt" / "linux-x86_64" / "bin"


def compile_sdk_platform() -> str:
    versions = KMP_DIR / "gradle" / "libs.versions.toml"
    try:
        return f"android-{tomllib.loads(versions.read_text())['versions']['android-compileSdk']}"
    except (OSError, KeyError, tomllib.TOMLDecodeError) as error:
        raise BuildError(f"could not read android-compileSdk from {versions}: {error}") from None


def rust_toolchain() -> tuple[str, list[str]]:
    config = tomllib.loads((ROOT / "rust-toolchain.toml").read_text())["toolchain"]
    return config["channel"], config.get("targets", [])


def rust_toolchain_installed(channel: str, env: dict) -> bool:
    toolchains = capture(["rustup", "toolchain", "list"], env=env)
    return re.search(rf"^{re.escape(channel)}-", toolchains, re.MULTILINE) is not None


def missing_rust_targets(channel: str, targets: list[str], env: dict) -> list[str]:
    installed = capture(["rustup", "target", "list", "--installed", "--toolchain", channel], env=env).split()
    return [target for target in targets if target not in installed]


def cargo_tool_version(name: str) -> str | None:
    exe = TOOLS_BIN / name
    if not exe.is_file():
        return None
    try:
        return capture([exe, "--version"]).split()[-1]
    except BuildError:
        return None



Step = tuple[str, Callable[[], str | None], Callable[[], None] | None]


def commands_problem(env: dict) -> str | None:
    missing = [name for name in REQUIRED_COMMANDS if shutil.which(name, path=env["PATH"]) is None]
    if missing:
        return f"missing {', '.join(missing)} (from the pixi `cross` env and rustup)"
    return None


def host_llvm_problem() -> str | None:
    config = HOST_LLVM / "bin" / "llvm-config"
    if not (config.is_file() and (HOST_LLVM / "bin" / "llvm-tblgen").is_file()):
        return f"not found in {HOST_LLVM}"
    version = capture([config, "--version"])
    return None if version.startswith("14.") else f"{HOST_LLVM} has LLVM {version}"


def install_host_llvm() -> None:
    if shutil.which("pixi") is None:
        raise BuildError(f"pixi not found; install LLVM 14 into {HOST_LLVM}")
    run(["pixi", "install", "--manifest-path", ROOT / "pixi.toml", "-e", "default"])


def rust_problem(env: dict) -> str | None:
    channel, targets = rust_toolchain()
    if not rust_toolchain_installed(channel, env):
        return f"toolchain {channel} not installed"
    missing = missing_rust_targets(channel, targets, env)
    return f"missing targets: {', '.join(missing)}" if missing else None


def install_rust(env: dict) -> None:
    channel, targets = rust_toolchain()
    if not rust_toolchain_installed(channel, env):
        run(["rustup", "toolchain", "install", channel, "--profile", "minimal"], env=env)
    if missing := missing_rust_targets(channel, targets, env):
        run(["rustup", "target", "add", "--toolchain", channel, *missing], env=env)


def cargo_tool_problem(name: str, version: str) -> str | None:
    current = cargo_tool_version(name)
    return None if current == version else f"found {current}"


def install_cargo_tool(name: str, version: str, env: dict) -> None:
    run(["cargo", "install", "--locked", "--root", TOOLS_DIR, f"{name}@{version}"], env=env)


def llvm_source_problem() -> str | None:
    return None if (LLVM_SRC / ".patched").is_file() else f"not ready in {LLVM_SRC}"


def install_llvm_source() -> None:
    if not (LLVM_SRC / "llvm" / "CMakeLists.txt").is_file():
        LLVM_SRC.parent.mkdir(parents=True, exist_ok=True)
        unpack_top_dir(download(LLVM_URL, LLVM_SHA256), LLVM_SRC, "llvm-project-*")
    small_vector = LLVM_SRC / "llvm" / "include" / "llvm" / "ADT" / "SmallVector.h"
    text = small_vector.read_text()
    if "#include <cstdint>" not in text:
        small_vector.write_text(text.replace("#include <cstddef>", "#include <cstddef>\n#include <cstdint>", 1))
    (LLVM_SRC / ".patched").touch()


def ndk_problem() -> str | None:
    ndk = android_ndk_dir()
    return None if (ndk / "source.properties").is_file() else f"not found in {ndk}"


def install_ndk() -> None:
    unpack_top_dir(download(NDK_URL, NDK_SHA256), android_ndk_dir(), "android-ndk-*")


def sdk_problem() -> str | None:
    sdk = android_sdk_dir()
    if (sdk / "platforms" / compile_sdk_platform()).is_dir() and (sdk / "build-tools" / SDK_BUILD_TOOLS).is_dir():
        return None
    return f"{compile_sdk_platform()} / build-tools {SDK_BUILD_TOOLS} not found in {sdk}"


def install_sdk(env: dict) -> None:
    sdkmanager = MANAGED_SDK / "cmdline-tools" / "latest" / "bin" / "sdkmanager"
    if not sdkmanager.is_file():
        (MANAGED_SDK / "cmdline-tools").mkdir(parents=True, exist_ok=True)
        unpack_top_dir(download(SDK_TOOLS_URL, SDK_TOOLS_SHA256), sdkmanager.parent.parent, "cmdline-tools")

    log("accepting Android SDK licenses")
    licenses = subprocess.run(
        [str(sdkmanager), f"--sdk_root={MANAGED_SDK}", "--licenses"],
        input="y\n" * 20, text=True, capture_output=True, env=env,
    )
    if licenses.returncode != 0:
        raise BuildError(f"sdkmanager --licenses failed:\n{licenses.stdout}\n{licenses.stderr}")
    packages = [f"platforms;{compile_sdk_platform()}", f"build-tools;{SDK_BUILD_TOOLS}"]
    run([sdkmanager, f"--sdk_root={MANAGED_SDK}", *packages], env=env)


def toolchain_steps(env: dict) -> list[Step]:
    steps: list[Step] = [
        ("host commands", partial(commands_problem, env), None),
        ("host LLVM 14", host_llvm_problem, install_host_llvm),
        ("Rust toolchain", partial(rust_problem, env), partial(install_rust, env)),
    ]
    for name, version in CARGO_TOOLS.items():
        steps.append((f"{name} {version}", partial(cargo_tool_problem, name, version),
                      partial(install_cargo_tool, name, version, env)))
    steps += [
        (f"LLVM {LLVM_VERSION} source", llvm_source_problem, install_llvm_source),
        ("Android NDK", ndk_problem, install_ndk),
        ("Android SDK", sdk_problem, partial(install_sdk, env)),
    ]
    return steps


def run_steps(steps: list[Step], check_only: bool) -> bool:
    ready = True
    for name, problem, install in steps:
        issue = problem()
        if issue is None:
            log(f"{name}: ok")
            continue
        if check_only or install is None:
            log(f"{name}: {issue}")
            ready = False
            continue
        log(f"{name}: {issue}; installing")
        install()
        if (issue := problem()) is not None:
            raise BuildError(f"{name} still not ready after installing: {issue}")
        log(f"{name}: installed")
    return ready


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="only detect; install nothing")
    args = parser.parse_args(argv)
    ensure_cross_env(__file__, argv)

    try:
        ready = run_steps(toolchain_steps(build_env()), args.check)
    except BuildError as error:
        return fail(error)
    if not ready:
        return fail(BuildError("toolchain incomplete (see above)"))
    log("toolchain ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
