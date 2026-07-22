from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
ACTIONLINT_SCRIPT = ROOT / "scripts" / "actionlint.sh"


def run_script_function(
    function_name: str,
    arguments: tuple[str, ...],
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "/bin/bash",
            "-c",
            'source "$1"; shift; "$@"',
            "actionlint-test",
            str(ACTIONLINT_SCRIPT),
            function_name,
            *arguments,
        ],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )


@pytest.mark.parametrize(
    ("system", "machine", "archive", "checksum"),
    [
        (
            "Darwin",
            "x86_64",
            "actionlint_1.7.9_darwin_amd64.tar.gz",
            "f89a910e90e536f60df7c504160247db01dd67cab6f08c064c1c397b76c91a79",
        ),
        (
            "Darwin",
            "arm64",
            "actionlint_1.7.9_darwin_arm64.tar.gz",
            "855e49e823fc68c6371fd6967e359cde11912d8d44fed343283c8e6e943bd789",
        ),
        (
            "Linux",
            "x86_64",
            "actionlint_1.7.9_linux_amd64.tar.gz",
            "233b280d05e100837f4af1433c7b40a5dcb306e3aa68fb4f17f8a7f45a7df7b4",
        ),
        (
            "Linux",
            "aarch64",
            "actionlint_1.7.9_linux_arm64.tar.gz",
            "6b82a3b8c808bf1bcd39a95aced22fc1a026eef08ede410f81e274af8deadbbc",
        ),
    ],
)
def test_release_metadata_matches_official_actionlint_assets(
    system: str,
    machine: str,
    archive: str,
    checksum: str,
) -> None:
    result = run_script_function("actionlint_release_metadata", (system, machine))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{archive} {checksum}"


def test_release_metadata_rejects_unsupported_platform() -> None:
    result = run_script_function(
        "actionlint_release_metadata", ("Windows_NT", "x86_64")
    )

    assert result.returncode != 0
    assert "does not support platform Windows_NT-x86_64" in result.stderr


def test_installed_actionlint_ignores_only_the_new_concurrency_queue_key(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "actionlint-call.log"
    actionlint = fake_bin / "actionlint"
    actionlint.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-version" ]; then printf "1.7.9\\n"; exit 0; fi\n'
        'printf "%s\\n" "$@" > "$ACTIONLINT_CALL_LOG"\n',
        encoding="utf-8",
    )
    actionlint.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "ACTIONLINT_CALL_LOG": str(call_log),
            "PATH": f"{fake_bin}{os.pathsep}/usr/bin:/bin",
        }
    )

    result = run_script_function("main", (".github/workflows/release.yml",), env)

    assert result.returncode == 0, result.stderr
    assert call_log.read_text(encoding="utf-8").splitlines() == [
        "-color",
        "-ignore",
        'unexpected key "queue" for "concurrency" section',
        ".github/workflows/release.yml",
    ]


@pytest.mark.parametrize(
    ("tool_name", "expected_arguments"),
    [
        ("sha256sum", "{archive}"),
        ("shasum", "-a 256 {archive}"),
    ],
)
def test_checksum_verification_uses_available_platform_tool(
    tmp_path: Path,
    tool_name: str,
    expected_arguments: str,
) -> None:
    expected_checksum = "a" * 64
    archive = tmp_path / "actionlint.tar.gz"
    archive.write_bytes(b"release archive")
    tool_log = tmp_path / "checksum-tool.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    checksum_tool = fake_bin / tool_name
    checksum_tool.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" > "$CHECKSUM_TOOL_LOG"\n'
        'printf "%s  archive\\n" "$EXPECTED_CHECKSUM"\n',
        encoding="utf-8",
    )
    checksum_tool.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "CHECKSUM_TOOL_LOG": str(tool_log),
            "EXPECTED_CHECKSUM": expected_checksum,
            "PATH": str(fake_bin),
        }
    )

    result = run_script_function(
        "verify_actionlint_checksum",
        (str(archive), expected_checksum),
        env,
    )

    assert result.returncode == 0, result.stderr
    assert tool_log.read_text(encoding="utf-8").strip() == expected_arguments.format(
        archive=archive
    )


def test_checksum_verification_fails_without_supported_tool(tmp_path: Path) -> None:
    archive = tmp_path / "actionlint.tar.gz"
    archive.write_bytes(b"release archive")
    env = os.environ.copy()
    env["PATH"] = str(tmp_path / "empty-bin")

    result = run_script_function(
        "verify_actionlint_checksum", (str(archive), "a" * 64), env
    )

    assert result.returncode != 0
    assert "requires 'sha256sum' or 'shasum'" in result.stderr


def test_bootstrap_cleans_up_download_after_success(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    download_dir = tmp_path / "actionlint-download"
    expected_checksum = (
        "233b280d05e100837f4af1433c7b40a5dcb306e3aa68fb4f17f8a7f45a7df7b4"
    )
    fake_tools = {
        "uname": """#!/bin/sh
if [ "$1" = "-s" ]; then printf 'Linux\\n'; else printf 'x86_64\\n'; fi
""",
        "mktemp": """#!/bin/sh
/bin/mkdir -p "$FAKE_TEMP_DIR"
printf '%s\\n' "$FAKE_TEMP_DIR"
""",
        "curl": """#!/bin/sh
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--output" ]; then shift; output="$1"; fi
  shift
done
printf 'archive' > "$output"
""",
        "sha256sum": """#!/bin/sh
printf '%s  %s\\n' "$EXPECTED_CHECKSUM" "$1"
""",
        "tar": """#!/bin/sh
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-C" ]; then shift; destination="$1"; fi
  shift
done
printf '#!/bin/sh\\nexit 0\\n' > "$destination/actionlint"
/bin/chmod +x "$destination/actionlint"
""",
        "rm": """#!/bin/sh
/bin/rm "$@"
""",
    }
    for name, content in fake_tools.items():
        tool = fake_bin / name
        tool.write_text(content, encoding="utf-8")
        tool.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "EXPECTED_CHECKSUM": expected_checksum,
            "FAKE_TEMP_DIR": str(download_dir),
            "PATH": str(fake_bin),
        }
    )

    result = run_script_function("main", (), env)

    assert result.returncode == 0, result.stderr
    assert not download_dir.exists()
