import json
import os
import shutil
import tomllib
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[3]


PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


PACKAGE_NAME = str(PYPROJECT["project"]["name"])


AUTHORED_VERSION = str(PYPROJECT["project"]["version"])


REPOSITORY_URL = "https://github.com/crewplaneai/crewplane"


UV_BOOTSTRAP_METADATA = json.loads(
    (ROOT / "packaging/uv-bootstrap.json").read_text(encoding="utf-8")
)


PINNED_UV_VERSION = str(UV_BOOTSTRAP_METADATA["version"])


UV_CHECKSUMS = {
    str(target): str(checksum)
    for target, checksum in UV_BOOTSTRAP_METADATA["checksums"].items()
}


UV_BOOTSTRAP_CASES = (
    (
        "Darwin",
        "arm64",
        "darwin",
        "arm64",
        "aarch64-apple-darwin",
        UV_CHECKSUMS["aarch64-apple-darwin"],
        "curl",
        "gnu",
    ),
    (
        "Darwin",
        "x86_64",
        "darwin",
        "x64",
        "x86_64-apple-darwin",
        UV_CHECKSUMS["x86_64-apple-darwin"],
        "wget",
        "gnu",
    ),
    (
        "Linux",
        "aarch64",
        "linux",
        "arm64",
        "aarch64-unknown-linux-gnu",
        UV_CHECKSUMS["aarch64-unknown-linux-gnu"],
        "curl",
        "gnu",
    ),
    (
        "Linux",
        "x86_64",
        "linux",
        "x64",
        "x86_64-unknown-linux-gnu",
        UV_CHECKSUMS["x86_64-unknown-linux-gnu"],
        "wget",
        "gnu",
    ),
    (
        "Linux",
        "aarch64",
        "linux",
        "arm64",
        "aarch64-unknown-linux-musl",
        UV_CHECKSUMS["aarch64-unknown-linux-musl"],
        "curl",
        "musl",
    ),
    (
        "Linux",
        "x86_64",
        "linux",
        "x64",
        "x86_64-unknown-linux-musl",
        UV_CHECKSUMS["x86_64-unknown-linux-musl"],
        "wget",
        "musl",
    ),
)


def extract_python_floor_from_pyproject() -> str:
    requires_python = str(PYPROJECT["project"]["requires-python"])
    specifier_set = SpecifierSet(requires_python)
    lower_bounds = [
        Version(spec.version) for spec in specifier_set if spec.operator in {">", ">="}
    ]
    assert lower_bounds
    return str(min(lower_bounds))


def repo_path(*parts: str) -> Path:
    return ROOT.joinpath(*parts)


def read_text(*parts: str) -> str:
    return repo_path(*parts).read_text(encoding="utf-8")


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def link_test_command(directory: Path, command: str) -> None:
    executable = shutil.which(command)
    assert executable is not None, f"{command} is required for installer tests"
    (directory / command).symlink_to(executable)


def prepare_uv_bootstrap_environment(
    tmp_path: Path,
    downloader: str,
    kernel: str,
    machine: str,
    target: str,
    actual_sha256: str,
    libc: str,
) -> tuple[dict[str, str], Path, Path, Path]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for command in ("awk", "basename", "chmod", "cp", "mkdir", "mktemp", "rm"):
        link_test_command(fake_bin, command)

    write_executable(
        fake_bin / "uname",
        """#!/bin/sh
case "${1:-}" in
    -s)
        if [ ! -e "$CREWPLANE_FAKE_PLATFORM_GATE_STATE" ]; then
            : > "$CREWPLANE_FAKE_PLATFORM_GATE_STATE"
            printf 'Darwin\n'
        else
            printf '%s\n' "$CREWPLANE_FAKE_KERNEL"
        fi
        ;;
    -m) printf '%s\n' "$CREWPLANE_FAKE_MACHINE" ;;
    *) exit 1 ;;
esac
""",
    )
    write_executable(
        fake_bin / "ldd",
        """#!/bin/sh
if [ "$CREWPLANE_FAKE_LIBC" = "musl" ]; then
    printf 'musl libc\n' >&2
else
    printf 'ldd (GNU libc) 2.39\n'
fi
""",
    )
    write_executable(
        fake_bin / "grep",
        """#!/bin/sh
case "$*" in
    *musl*) [ "$CREWPLANE_FAKE_LIBC" = "musl" ] ;;
    *) exit 1 ;;
esac
""",
    )
    write_executable(
        fake_bin / downloader,
        """#!/bin/sh
if [ "${1:-}" = "--version" ]; then
    exit 0
fi
output=""
url=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        -o|--output|-O|-qO|--output-document)
            output="$2"
            shift 2
            ;;
        --output-document=*)
            output="${1#*=}"
            shift
            ;;
        http*)
            url="$1"
            shift
            ;;
        *)
            shift
            ;;
    esac
done
[ -n "$output" ] || exit 1
printf 'archive fixture' > "$output"
printf '%s\n' "$url" >> "$CREWPLANE_FAKE_DOWNLOAD_LOG"
""",
    )
    write_executable(
        fake_bin / "sha256sum",
        """#!/bin/sh
printf '%s  %s\n' "$CREWPLANE_FAKE_ACTUAL_SHA256" "$1"
""",
    )
    write_executable(
        fake_bin / "tar",
        """#!/bin/sh
destination=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        -C)
            destination="$2"
            shift 2
            ;;
        *)
            shift
            ;;
    esac
done
[ -n "$destination" ] || exit 1
archive_dir="$destination/uv-$CREWPLANE_FAKE_UV_TARGET"
/bin/mkdir -p "$archive_dir"
/bin/cp "$CREWPLANE_FAKE_UV" "$archive_dir/uv"
/bin/cp "$CREWPLANE_FAKE_UV" "$archive_dir/uvx"
/bin/chmod 0755 "$archive_dir/uv" "$archive_dir/uvx"
""",
    )

    fake_uv = tmp_path / "fake-uv"
    write_executable(
        fake_uv,
        """#!/bin/sh
{
    printf 'CALL'
    for arg in "$@"; do printf '\t%s' "$arg"; done
    printf '\n'
} >> "$CREWPLANE_FAKE_UV_LOG"
if [ "${1:-}" = "tool" ] && [ "${2:-}" = "dir" ]; then
    printf '%s\n' "$CREWPLANE_FAKE_TOOL_BIN"
fi
""",
    )

    tool_bin = tmp_path / "tool-bin"
    tool_bin.mkdir()
    fake_cli = tool_bin / PACKAGE_NAME
    write_executable(fake_cli, "#!/bin/sh\nexit 0\n")
    (fake_bin / PACKAGE_NAME).symlink_to(fake_cli)

    install_home = tmp_path / "home"
    install_home.mkdir()
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()
    download_log = tmp_path / "download.log"
    uv_log = tmp_path / "uv.log"

    env = os.environ.copy()
    for name in ("CREWPLANE_UV_BIN", "UV_INSTALL_DIR", "UV_UNMANAGED_INSTALL"):
        env.pop(name, None)
    env.update(
        {
            "CREWPLANE_FAKE_ACTUAL_SHA256": actual_sha256,
            "CREWPLANE_FAKE_DOWNLOAD_LOG": str(download_log),
            "CREWPLANE_FAKE_KERNEL": kernel,
            "CREWPLANE_FAKE_LIBC": libc,
            "CREWPLANE_FAKE_MACHINE": machine,
            "CREWPLANE_FAKE_PLATFORM_GATE_STATE": str(tmp_path / "platform-gate.state"),
            "CREWPLANE_FAKE_TOOL_BIN": str(tool_bin),
            "CREWPLANE_FAKE_UV": str(fake_uv),
            "CREWPLANE_FAKE_UV_LOG": str(uv_log),
            "CREWPLANE_FAKE_UV_TARGET": target,
            "CREWPLANE_INSTALL_HOME": str(install_home),
            "HOME": str(install_home),
            "PATH": str(fake_bin),
            "TMPDIR": str(temp_dir),
        }
    )
    return env, install_home, temp_dir, download_log


def load_pyproject() -> dict[str, object]:
    return PYPROJECT
