#!/usr/bin/env python3
"""Check and install everything the cross builds need, on a Linux x86_64 host.

    python3 scripts/install_toolchain.py           # detect, install what is missing
    python3 scripts/install_toolchain.py --check   # detect only, exit 1 if anything is missing

What it covers, in order (each step is skipped when already in place):
  1. host commands from the pixi `cross` env (cmake, ninja, zig, clang-cl, ...)
  2. host LLVM 14 (pixi `default` env): llvm-tblgen / llvm-config for the cross builds
  3. the Rust toolchain and targets pinned by rust-toolchain.toml
  4. cargo-xwin, pinned, in .pixi/tools
  5. LLVM 14 source (downloaded, checksum-verified, patched)
  6. Android NDK r30
  7. Android SDK command-line tools + the platform / build-tools libkmp needs

Normally run through `just setup-cross` (run_workflow.py setup). The build
scripts (build_llvm_*.py, run_workflow.py) import the shared paths and helpers
from this file, so there is a single place for them.
"""

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
from pathlib import Path

# --------------------------------------------------------------------------
# Paths and versions (environment variables override the defaults)
# --------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
TOOLCHAINS = Path(os.environ.get("TOOLCHAINS_DIR", ROOT / ".toolchains"))
DOWNLOADS = TOOLCHAINS / "downloads"
TOOLS_DIR = ROOT / ".pixi" / "tools"  # `cargo install --root` for pinned cargo tools
TOOLS_BIN = TOOLS_DIR / "bin"
KMP_DIR = ROOT / "libkmp"
ARTIFACT = os.environ.get("ARTIFACT", "mun")

LLVM_VERSION = "14.0.6"
LLVM_URL = (
    "https://github.com/llvm/llvm-project/releases/download/"
    f"llvmorg-{LLVM_VERSION}/llvm-project-{LLVM_VERSION}.src.tar.xz"
)
LLVM_SHA256 = "8b3cfd7bc695bd6cea0f37f53f0981f34f87496e79e2529874fd03a2f9dd3a8a"
LLVM_SRC = Path(os.environ.get("LLVM_SRC", TOOLCHAINS / "src" / f"llvm-project-{LLVM_VERSION}.src"))
# Cross-built LLVM installs: <root>/<android|linux|windows>/<abi or rust target>
LLVM_INSTALL_ROOT = Path(os.environ.get("LLVM14_ROOT", TOOLCHAINS / "llvm14"))
LLVM_BUILD_ROOT = Path(os.environ.get("LLVM14_BUILD", TOOLCHAINS / "build"))
# Host LLVM 14 (pixi `default` env): its llvm-tblgen drives the cross builds and
# it is what the host library / bindings link against.
HOST_LLVM = Path(os.environ.get("LLVM14_HOST", ROOT / ".pixi" / "envs" / "default"))

NDK_URL = "https://dl.google.com/android/repository/android-ndk-r30-linux.zip"
NDK_SHA256 = "753611f410d002cfcd3f3dc2ef49aad532089d3180b436c060a90bf0fcb64df2"
MANAGED_NDK = TOOLCHAINS / "android-ndk-r30"
ANDROID_API = os.environ.get("ANDROID_API", "24")

SDK_TOOLS_URL = "https://dl.google.com/android/repository/commandlinetools-linux-13114758_latest.zip"
SDK_TOOLS_SHA256 = "7ec965280a073311c339e571cd5de778b9975026cfcbe79f2b1cdcb1e15317ee"
MANAGED_SDK = Path(os.environ.get("SDK_DIR", TOOLCHAINS / "android-sdk"))
SDK_BUILD_TOOLS = "36.0.0"

# gobley-uniffi-bindgen is installed by the justfile (generate-bindings), on every OS.
CARGO_TOOLS = {"cargo-xwin": "0.23.1"}

# Commands the pixi `cross` env provides (plus rustup/cargo from the user).
REQUIRED_COMMANDS = [
    "cargo", "rustup", "cmake", "ninja", "zig", "cargo-zigbuild",
    "clang-cl", "lld-link", "llvm-lib", "llvm-rc", "llvm-mt", "java",
]

JOBS = os.environ.get("JOBS", str(os.cpu_count() or 4))


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


class BuildError(Exception):
    """A step failed; the message says what went wrong and, if possible, what to do."""


def log(message: str) -> None:
    print(f"==> {message}", flush=True)


def run(cmd: list, env: dict | None = None, cwd: Path | None = None) -> None:
    """Run a command, streaming its output; raise BuildError if it fails."""
    cmd = [str(part) for part in cmd]
    print(f"+ {shlex.join(cmd)}", flush=True)
    try:
        result = subprocess.run(cmd, env=env, cwd=cwd)
    except FileNotFoundError:
        raise BuildError(f"command not found: {cmd[0]}") from None
    if result.returncode != 0:
        raise BuildError(f"{Path(cmd[0]).name} exited with code {result.returncode} (command shown above)")


def capture(cmd: list, env: dict | None = None, cwd: Path | None = None) -> str:
    """Run a command and return its stdout; raise BuildError if it fails."""
    cmd = [str(part) for part in cmd]
    try:
        result = subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True)
    except FileNotFoundError:
        raise BuildError(f"command not found: {cmd[0]}") from None
    if result.returncode != 0:
        raise BuildError(
            f"exit code {result.returncode}: {shlex.join(cmd)}\n{result.stderr.strip()}"
        )
    return result.stdout.strip()


def build_env() -> dict[str, str]:
    """os.environ with the pinned cargo tools (.pixi/tools/bin) first on PATH."""
    env = os.environ.copy()
    env["PATH"] = f"{TOOLS_BIN}{os.pathsep}{env.get('PATH', '')}"
    return env


def ensure_cross_env(script: str, argv: list[str]) -> None:
    """Re-run `script` inside the pixi `cross` environment unless already there.

    The cross tools (cmake, ninja, zig, clang-cl, ...) and the LLVM14_HOST
    variable come from that environment. Without pixi, tools are taken from PATH.
    Exits early with a clear message on hosts that cannot cross-build at all.
    """
    try:
        require_host_linux()
    except BuildError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
    if os.environ.get("PIXI_ENVIRONMENT_NAME") == "cross":
        return
    pixi = shutil.which("pixi")
    manifest = ROOT / "pixi.toml"
    if pixi is None or not manifest.is_file():
        log("pixi not found: using the tools on PATH instead of the pixi `cross` env")
        return
    cmd = [pixi, "run", "--manifest-path", str(manifest), "-e", "cross",
           sys.executable, str(Path(script).resolve()), *argv]
    log("entering the pixi `cross` environment")
    sys.exit(subprocess.run(cmd).returncode)


def write_file(path: Path, text: str, executable: bool = False) -> None:
    """Write `text` to `path` unless it already has exactly that content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file() or path.read_text() != text:
        path.write_text(text)
    if executable:
        path.chmod(0o755)


def require_host_linux() -> None:
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "AMD64"):
        raise BuildError(
            f"the cross builds need a Linux x86_64 host (this is {platform.system()} {platform.machine()})"
        )


# --------------------------------------------------------------------------
# Shared LLVM 14 cross-build helpers
# --------------------------------------------------------------------------

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


def llvm_prefix(platform_name: str, name: str) -> Path:
    return LLVM_INSTALL_ROOT / platform_name / name


def llvm_build_dir(platform_name: str, name: str) -> Path:
    return LLVM_BUILD_ROOT / platform_name / name


def llvm_installed(prefix: Path) -> bool:
    """llvm-config is written last, so it marks a finished install."""
    return (prefix / "bin" / "llvm-config").is_file()


def require_llvm(prefix: Path, hint: str) -> None:
    if not llvm_installed(prefix):
        raise BuildError(f"no LLVM 14 in {prefix}; run: {hint}")


def require_llvm_source() -> None:
    if not (LLVM_SRC / ".patched").is_file():
        raise BuildError(f"LLVM source not ready in {LLVM_SRC}; run: python3 scripts/install_toolchain.py")


def llvm_cmake_args(build: Path, prefix: Path, triple: str, targets_to_build: str) -> list[str]:
    """CMake arguments every LLVM 14 + lld cross build shares."""
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
        version=LLVM_VERSION, cflags=cflags, cxxflags=cxxflags,
        libnames=libnames, system_libs=system_libs,
    )
    write_file(prefix / "bin" / "llvm-config", text, executable=True)


# --------------------------------------------------------------------------
# Downloads and archives
# --------------------------------------------------------------------------


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, sha256: str) -> Path:
    """Download `url` into .toolchains/downloads (cached, checksum-verified)."""
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    dest = DOWNLOADS / url.rsplit("/", 1)[-1]
    if dest.is_file():
        if sha256_of(dest) == sha256:
            log(f"using cached {dest.name}")
            return dest
        log(f"checksum mismatch, downloading {dest.name} again")
        dest.unlink()

    part_file = dest.with_name(dest.name + ".part")
    attempts = 3
    for attempt in range(1, attempts + 1):
        log(f"downloading {url} (attempt {attempt}/{attempts})")
        try:
            with urllib.request.urlopen(url, timeout=60) as response, part_file.open("wb") as out:
                shutil.copyfileobj(response, out, 1 << 20)
        except OSError as error:
            part_file.unlink(missing_ok=True)
            if attempt == attempts:
                raise BuildError(f"could not download {url}: {error}") from None
            log(f"download failed ({error}), retrying")
            time.sleep(2 * attempt)
            continue
        actual = sha256_of(part_file)
        if actual != sha256:
            part_file.unlink()
            raise BuildError(f"checksum mismatch for {url}: expected {sha256}, got {actual}")
        part_file.replace(dest)
        return dest
    raise BuildError(f"could not download {url}")


def extract_zip(archive: Path, dest: Path) -> None:
    """Unzip keeping Unix permissions and symlinks (zipfile.extractall drops both,
    which leaves the NDK's compilers and the SDK's sdkmanager unusable)."""
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            parts = Path(info.filename).parts
            if info.filename.startswith("/") or ".." in parts:
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
    """Extract `archive` to a staging dir, then move its top-level dir to `dest`.

    `dest` only appears once extraction is complete, so an interrupted run never
    leaves a half-extracted toolchain that looks installed.
    """
    staging = dest.parent / f".{dest.name}.partial"
    if staging.exists():
        shutil.rmtree(staging)  # leftover from an interrupted run of this script
    extract_archive(archive, staging)
    matches = list(staging.glob(top_dir_glob))
    if len(matches) != 1:
        raise BuildError(f"expected one {top_dir_glob} directory in {archive}, found {len(matches)}")
    matches[0].rename(dest)
    shutil.rmtree(staging)


# --------------------------------------------------------------------------
# Detection of existing installations
# --------------------------------------------------------------------------


def android_ndk_dir() -> Path:
    """$NDK_DIR, else the managed NDK, else $ANDROID_NDK_HOME, else where the managed one goes."""
    if "NDK_DIR" in os.environ:
        return Path(os.environ["NDK_DIR"])
    if (MANAGED_NDK / "source.properties").is_file():
        return MANAGED_NDK
    home = os.environ.get("ANDROID_NDK_HOME")
    if home and (Path(home) / "source.properties").is_file():
        return Path(home)
    return MANAGED_NDK


def android_sdk_dir() -> Path:
    """The managed SDK when present, else $ANDROID_HOME, else where the managed one goes."""
    if (MANAGED_SDK / "platforms").is_dir():
        return MANAGED_SDK
    home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if home and (Path(home) / "platforms").is_dir():
        return Path(home)
    return MANAGED_SDK


def ndk_bin_dir() -> Path:
    return android_ndk_dir() / "toolchains" / "llvm" / "prebuilt" / "linux-x86_64" / "bin"


def compile_sdk_version() -> str:
    versions = KMP_DIR / "gradle" / "libs.versions.toml"
    try:
        return str(tomllib.loads(versions.read_text())["versions"]["android-compileSdk"])
    except (OSError, KeyError, tomllib.TOMLDecodeError) as error:
        raise BuildError(f"could not read android-compileSdk from {versions}: {error}") from None


def rust_toolchain() -> tuple[str, list[str]]:
    """(channel, targets) pinned by rust-toolchain.toml."""
    config = tomllib.loads((ROOT / "rust-toolchain.toml").read_text())["toolchain"]
    return config["channel"], config.get("targets", [])


def cargo_tool_version(name: str) -> str | None:
    exe = TOOLS_BIN / name
    if not exe.is_file():
        return None
    try:
        return capture([exe, "--version"]).split()[-1]
    except BuildError:
        return None


# --------------------------------------------------------------------------
# Install steps: each returns True when the component is ready
# --------------------------------------------------------------------------


def check_commands(env: dict) -> bool:
    missing = [name for name in REQUIRED_COMMANDS if shutil.which(name, path=env["PATH"]) is None]
    if not missing:
        log("host commands: ok")
        return True
    log(f"host commands missing: {', '.join(missing)}")
    log("they come from the pixi `cross` env (pixi install -e cross) and rustup")
    return False


def ensure_host_llvm(check_only: bool) -> bool:
    tblgen = HOST_LLVM / "bin" / "llvm-tblgen"
    config = HOST_LLVM / "bin" / "llvm-config"
    if not (tblgen.is_file() and config.is_file()):
        if check_only or shutil.which("pixi") is None:
            log(f"host LLVM 14 missing in {HOST_LLVM} (pixi install -e default)")
            return False
        run(["pixi", "install", "--manifest-path", ROOT / "pixi.toml", "-e", "default"])
    version = capture([config, "--version"])
    if not version.startswith("14."):
        log(f"host LLVM in {HOST_LLVM} is {version}, LLVM 14 is required")
        return False
    log(f"host LLVM {version}: ok ({HOST_LLVM})")
    return True


def ensure_rust_targets(env: dict, check_only: bool) -> bool:
    channel, targets = rust_toolchain()
    installed_toolchains = capture(["rustup", "toolchain", "list"], env=env)
    if not re.search(rf"^{re.escape(channel)}-", installed_toolchains, re.MULTILINE):
        if check_only:
            log(f"Rust toolchain {channel} missing")
            return False
        run(["rustup", "toolchain", "install", channel, "--profile", "minimal"], env=env)

    installed = capture(["rustup", "target", "list", "--installed", "--toolchain", channel], env=env).split()
    missing = [target for target in targets if target not in installed]
    if missing:
        if check_only:
            log(f"Rust targets missing: {', '.join(missing)}")
            return False
        run(["rustup", "target", "add", "--toolchain", channel, *missing], env=env)
    log(f"Rust {channel} with {len(targets)} targets: ok")
    return True


def ensure_cargo_tools(env: dict, check_only: bool) -> bool:
    ready = True
    for name, version in CARGO_TOOLS.items():
        current = cargo_tool_version(name)
        if current == version:
            log(f"{name} {version}: ok")
            continue
        if check_only:
            log(f"{name} {version} missing (found: {current})")
            ready = False
            continue
        run(["cargo", "install", "--locked", "--root", TOOLS_DIR, f"{name}@{version}"], env=env)
    return ready


def ensure_llvm_source(check_only: bool) -> bool:
    marker = LLVM_SRC / ".patched"
    if marker.is_file():
        log(f"LLVM {LLVM_VERSION} source: ok ({LLVM_SRC})")
        return True
    if check_only:
        log(f"LLVM {LLVM_VERSION} source missing in {LLVM_SRC}")
        return False

    if not (LLVM_SRC / "llvm" / "CMakeLists.txt").is_file():
        tarball = download(LLVM_URL, LLVM_SHA256)
        LLVM_SRC.parent.mkdir(parents=True, exist_ok=True)
        unpack_top_dir(tarball, LLVM_SRC, "llvm-project-*")

    # LLVM 14 predates GCC 13 / recent clang, which no longer pull <cstdint> in
    # transitively; SmallVector.h uses uint32_t/uint64_t.
    small_vector = LLVM_SRC / "llvm" / "include" / "llvm" / "ADT" / "SmallVector.h"
    text = small_vector.read_text()
    if "#include <cstdint>" not in text:
        small_vector.write_text(text.replace("#include <cstddef>", "#include <cstddef>\n#include <cstdint>", 1))
    marker.touch()
    log(f"LLVM {LLVM_VERSION} source ready: {LLVM_SRC}")
    return True


def ensure_android_ndk(check_only: bool) -> bool:
    ndk = android_ndk_dir()
    if (ndk / "source.properties").is_file():
        log(f"Android NDK: ok ({ndk})")
        return True
    if check_only:
        log(f"Android NDK missing in {ndk}")
        return False
    archive = download(NDK_URL, NDK_SHA256)
    unpack_top_dir(archive, ndk, "android-ndk-*")
    log(f"Android NDK installed: {ndk}")
    return True


def ensure_android_sdk(env: dict, check_only: bool) -> bool:
    platform_package = f"android-{compile_sdk_version()}"
    sdk = android_sdk_dir()
    if (sdk / "platforms" / platform_package).is_dir() and (sdk / "build-tools" / SDK_BUILD_TOOLS).is_dir():
        log(f"Android SDK ({platform_package}, build-tools {SDK_BUILD_TOOLS}): ok ({sdk})")
        return True
    if check_only:
        log(f"Android SDK {platform_package} / build-tools {SDK_BUILD_TOOLS} missing in {sdk}")
        return False

    sdk = MANAGED_SDK
    sdkmanager = sdk / "cmdline-tools" / "latest" / "bin" / "sdkmanager"
    if not sdkmanager.is_file():
        archive = download(SDK_TOOLS_URL, SDK_TOOLS_SHA256)
        (sdk / "cmdline-tools").mkdir(parents=True, exist_ok=True)
        unpack_top_dir(archive, sdk / "cmdline-tools" / "latest", "cmdline-tools")

    log("accepting Android SDK licenses")
    licenses = subprocess.run(
        [str(sdkmanager), f"--sdk_root={sdk}", "--licenses"],
        input="y\n" * 20, text=True, capture_output=True, env=env,
    )
    if licenses.returncode != 0:
        raise BuildError(f"sdkmanager --licenses failed:\n{licenses.stdout}\n{licenses.stderr}")
    run([sdkmanager, f"--sdk_root={sdk}", f"platforms;{platform_package}", f"build-tools;{SDK_BUILD_TOOLS}"], env=env)
    log(f"Android SDK installed: {sdk}")
    return True


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="only detect; install nothing")
    args = parser.parse_args(argv)
    ensure_cross_env(__file__, argv)

    try:
        env = build_env()
        results = [
            check_commands(env),
            ensure_host_llvm(args.check),
            ensure_rust_targets(env, args.check),
            ensure_cargo_tools(env, args.check),
            ensure_llvm_source(args.check),
            ensure_android_ndk(args.check),
            ensure_android_sdk(env, args.check),
        ]
    except BuildError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if not all(results):
        print("error: toolchain incomplete (see above)", file=sys.stderr)
        return 1
    log("toolchain ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
