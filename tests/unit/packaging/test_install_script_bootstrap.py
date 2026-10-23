import os
import shutil
from pathlib import Path

import pytest

from tests.helpers.processes import run_process
from tests.unit.packaging.release_surfaces_support import (
    AUTHORED_VERSION,
    PACKAGE_NAME,
    PINNED_UV_VERSION,
    ROOT,
    UV_BOOTSTRAP_CASES,
    prepare_uv_bootstrap_environment,
    read_text,
    repo_path,
)


def test_install_script_uses_uv_and_supports_local_artifact_smoke() -> None:
    installer = read_text("install.sh")
    assert 'PACKAGE_NAME="crewplane"' in installer
    assert "CLI_NAME" not in installer
    assert 'CREWPLANE_VERSION="${CREWPLANE_VERSION:-}"' in installer
    assert 'package_spec="$PACKAGE_NAME"' in installer
    assert 'package_spec="${PACKAGE_NAME}==${CREWPLANE_VERSION}"' in installer
    assert f"CREWPLANE_VERSION:-{AUTHORED_VERSION}" not in installer
    assert "CREWPLANE_INSTALL_FIND_LINKS" in installer
    assert "CREWPLANE_INSTALL_NO_INDEX" in installer
    assert "CREWPLANE_INSTALL_HOME" in installer
    assert "CREWPLANE_INSTALL_PYTHON" in installer
    assert "tool install --force" in installer
    assert "--find-links" in installer
    assert "--no-index" in installer
    assert "tool dir --bin" in installer
    assert "export PATH=" in installer
    assert "${PACKAGE_NAME} --help" in installer
    assert "uv tool uninstall ${PACKAGE_NAME}" in installer
    assert "native Windows is not supported" in installer


@pytest.mark.parametrize(
    (
        "kernel",
        "machine",
        "node_platform",
        "node_arch",
        "target",
        "sha256",
        "downloader",
        "libc",
    ),
    UV_BOOTSTRAP_CASES,
)
def test_install_script_bootstraps_verified_uv_archive(
    tmp_path: Path,
    kernel: str,
    machine: str,
    node_platform: str,
    node_arch: str,
    target: str,
    sha256: str,
    downloader: str,
    libc: str,
) -> None:
    del node_platform, node_arch
    if os.name == "nt":
        pytest.skip("native Windows is outside the supported installer surface")
    shell = shutil.which("sh")
    assert shell is not None
    env, install_home, temp_dir, download_log = prepare_uv_bootstrap_environment(
        tmp_path,
        downloader,
        kernel,
        machine,
        target,
        sha256,
        libc,
    )

    result = run_process(
        [shell, str(repo_path("install.sh"))], cwd=ROOT, env=env, check=False
    )

    assert result.returncode == 0, result.stderr
    expected_url = f"https://github.com/astral-sh/uv/releases/download/{PINNED_UV_VERSION}/uv-{target}.tar.gz"
    assert download_log.read_text(encoding="utf-8").strip() == expected_url
    assert (install_home / ".local" / "bin" / "uv").stat().st_mode & 0o111
    assert (install_home / ".local" / "bin" / "uvx").stat().st_mode & 0o111
    assert not any(temp_dir.iterdir())


def test_install_script_rejects_uv_archive_checksum_mismatch(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("native Windows is outside the supported installer surface")
    shell = shutil.which("sh")
    assert shell is not None
    target = "x86_64-unknown-linux-gnu"
    env, install_home, temp_dir, _ = prepare_uv_bootstrap_environment(
        tmp_path,
        "curl",
        "Linux",
        "x86_64",
        target,
        "0" * 64,
        "gnu",
    )

    result = run_process(
        [shell, str(repo_path("install.sh"))], cwd=ROOT, env=env, check=False
    )

    assert result.returncode == 1
    assert "checksum" in result.stderr.lower()
    assert not (install_home / ".local" / "bin" / "uv").exists()
    assert not any(temp_dir.iterdir())


@pytest.mark.parametrize(
    ("version_override", "expected_package_spec"),
    [
        (None, PACKAGE_NAME),
        ("9.8.7", f"{PACKAGE_NAME}==9.8.7"),
    ],
)
@pytest.mark.parametrize("python_path", [None, "/python path/bin/python"])
@pytest.mark.parametrize("find_links", [None, "/local wheels"])
@pytest.mark.parametrize("no_index", [None, "", "0", "1", "other"])
@pytest.mark.parametrize("exit_code", [0, 7])
def test_install_script_preserves_install_arguments_and_exit_status(
    tmp_path: Path,
    version_override: str | None,
    expected_package_spec: str,
    python_path: str | None,
    find_links: str | None,
    no_index: str | None,
    exit_code: int,
) -> None:
    fake_uv = tmp_path / "fake uv"
    fake_uv_log = tmp_path / "uv.log"
    tool_bin = tmp_path / "bin"
    tool_bin.mkdir()
    fake_cli = tool_bin / PACKAGE_NAME
    fake_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_cli.chmod(0o755)
    fake_uv.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                "{",
                "  printf 'CALL'",
                '  for arg in "$@"; do printf "\\t%s" "$arg"; done',
                "  printf '\\n'",
                '} >> "$CREWPLANE_FAKE_UV_LOG"',
                'if [ "$1" = "tool" ] && [ "$2" = "install" ]; then',
                '  exit "$CREWPLANE_FAKE_UV_EXIT_CODE"',
                "fi",
                'if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then',
                '  printf "%s\\n" "$CREWPLANE_FAKE_TOOL_BIN"',
                "fi",
            ]
        ),
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    env = os.environ.copy()
    for name in (
        "CREWPLANE_VERSION",
        "CREWPLANE_INSTALL_FIND_LINKS",
        "CREWPLANE_INSTALL_NO_INDEX",
        "CREWPLANE_INSTALL_PYTHON",
    ):
        env.pop(name, None)
    env["CREWPLANE_UV_BIN"] = str(fake_uv)
    env["CREWPLANE_FAKE_UV_LOG"] = str(fake_uv_log)
    env["CREWPLANE_FAKE_TOOL_BIN"] = str(tool_bin)
    env["CREWPLANE_FAKE_UV_EXIT_CODE"] = str(exit_code)
    env["CREWPLANE_INSTALL_HOME"] = str(tmp_path / "home")
    env["PATH"] = f"{tool_bin}{os.pathsep}{env.get('PATH', '')}"
    if version_override is not None:
        env["CREWPLANE_VERSION"] = version_override
    if python_path is not None:
        env["CREWPLANE_INSTALL_PYTHON"] = python_path
    if find_links is not None:
        env["CREWPLANE_INSTALL_FIND_LINKS"] = find_links
    if no_index is not None:
        env["CREWPLANE_INSTALL_NO_INDEX"] = no_index

    result = run_process(
        ["sh", str(repo_path("install.sh"))], cwd=ROOT, env=env, check=False
    )

    assert result.returncode == exit_code, result.stderr
    calls = [
        line.split("\t")
        for line in fake_uv_log.read_text(encoding="utf-8").splitlines()
    ]
    expected = ["CALL", "tool", "install", "--force"]
    if python_path is not None:
        expected.extend(["--python", python_path])
    if find_links is not None:
        expected.extend(["--find-links", find_links])
        if no_index != "0":
            expected.append("--no-index")
    assert calls[0] == [*expected, expected_package_spec]
    if exit_code:
        assert len(calls) == 1
