import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.packaging.release_surfaces_support import (
    AUTHORED_VERSION,
    PACKAGE_NAME,
    PINNED_UV_VERSION,
    ROOT,
    UV_BOOTSTRAP_CASES,
    extract_python_floor_from_pyproject,
    prepare_uv_bootstrap_environment,
    repo_path,
)


def npm_bootstrap_command(
    node_platform: str,
    node_arch: str,
    mock_crypto: bool,
    node_libc: str = "gnu",
) -> str:
    statements = [
        f"Object.defineProperty(process, 'platform', {{ value: '{node_platform}' }});",
        f"Object.defineProperty(process, 'arch', {{ value: '{node_arch}' }});",
    ]
    if node_platform == "linux":
        report_header = (
            "{ glibcVersionRuntime: '2.39' }" if node_libc == "gnu" else "{}"
        )
        statements.append(
            f"process.report.getReport = () => ({{ header: {report_header} }});"
        )
    if mock_crypto:
        statements.extend(
            [
                "const Module = require('node:module');",
                "const originalLoad = Module._load;",
                "Module._load = function(request, parent, isMain) {",
                "  if (request === 'node:crypto') {",
                "    return { createHash() { return {",
                "      update() { return this; },",
                "      digest() { return process.env.CREWPLANE_FAKE_ACTUAL_SHA256; },",
                "    }; } };",
                "  }",
                "  return originalLoad.call(this, request, parent, isMain);",
                "};",
            ]
        )
    statements.append("require('./packaging/npm/scripts/postinstall.js');")
    return "".join(statements)


@pytest.mark.parametrize(
    (
        "kernel",
        "machine",
        "node_platform",
        "node_arch",
        "target",
        "sha256",
        "downloader",
        "node_libc",
    ),
    UV_BOOTSTRAP_CASES,
)
def test_npm_postinstall_bootstraps_verified_uv_archive(
    tmp_path: Path,
    kernel: str,
    machine: str,
    node_platform: str,
    node_arch: str,
    target: str,
    sha256: str,
    downloader: str,
    node_libc: str,
) -> None:
    if os.name == "nt":
        pytest.skip("native Windows is outside the supported npm surface")
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the npm postinstall regression")
    env, install_home, temp_dir, download_log = prepare_uv_bootstrap_environment(
        tmp_path,
        downloader,
        kernel,
        machine,
        target,
        sha256,
        node_libc,
    )
    env.pop("CREWPLANE_INSTALL_HOME")

    result = subprocess.run(
        [
            node,
            "-e",
            npm_bootstrap_command(node_platform, node_arch, True, node_libc),
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    expected_url = f"https://github.com/astral-sh/uv/releases/download/{PINNED_UV_VERSION}/uv-{target}.tar.gz"
    assert download_log.read_text(encoding="utf-8").strip() == expected_url
    assert (install_home / ".local" / "bin" / "uv").stat().st_mode & 0o111
    assert (install_home / ".local" / "bin" / "uvx").stat().st_mode & 0o111
    assert not any(temp_dir.iterdir())


def test_npm_postinstall_rejects_uv_archive_checksum_mismatch(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("native Windows is outside the supported npm surface")
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the npm postinstall regression")
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
    env.pop("CREWPLANE_INSTALL_HOME")

    result = subprocess.run(
        [node, "-e", npm_bootstrap_command("linux", "x64", False)],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "checksum" in result.stderr.lower()
    assert not (install_home / ".local" / "bin" / "uv").exists()
    assert not any(temp_dir.iterdir())


def test_npm_postinstall_defaults_to_min_supported_python_without_override(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("native Windows is outside the supported npm smoke surface")
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the npm postinstall regression")

    fake_uv = tmp_path / "uv"
    fake_uv_log = tmp_path / "uv.log"
    fake_uv.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                "{",
                "  printf 'CALL'",
                '  for arg in "$@"; do printf "\\t%s" "$arg"; done',
                "  printf '\\n'",
                '} >> "$CREWPLANE_FAKE_UV_LOG"',
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

    subprocess.run(
        [node, str(repo_path("packaging", "npm", "scripts", "postinstall.js"))],
        cwd=ROOT,
        env=env,
        check=True,
    )

    calls = [
        line.split("\t")
        for line in fake_uv_log.read_text(encoding="utf-8").splitlines()
    ]
    default_python = extract_python_floor_from_pyproject()
    assert calls[0][:4] == ["CALL", "venv", "--python", default_python]
    assert calls[0][4] == str(repo_path("packaging", "npm", ".venv"))
    assert calls[1][:4] == [
        "CALL",
        "pip",
        "install",
        "--python",
    ]
    assert calls[1][-1] == f"{PACKAGE_NAME}=={AUTHORED_VERSION}"


def test_npm_postinstall_rejects_native_windows_before_uv_lookup(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the npm postinstall regression")

    fake_uv = tmp_path / "uv"
    fake_uv_log = tmp_path / "uv.log"
    fake_uv.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                "printf 'uv called\\n' >> \"$CREWPLANE_FAKE_UV_LOG\"",
            ]
        ),
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    env = os.environ.copy()
    env["CREWPLANE_UV_BIN"] = str(fake_uv)
    env["CREWPLANE_FAKE_UV_LOG"] = str(fake_uv_log)

    result = subprocess.run(
        [
            node,
            "-e",
            (
                "Object.defineProperty(process, 'platform', { value: 'win32' });"
                "require('./packaging/npm/scripts/postinstall.js');"
            ),
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "native Windows is not supported" in result.stderr
    assert not fake_uv_log.exists()


def test_npm_bin_rejects_native_windows_before_venv_lookup() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the npm bin regression")

    result = subprocess.run(
        [
            node,
            "-e",
            (
                "Object.defineProperty(process, 'platform', { value: 'win32' });"
                "require('./packaging/npm/bin/crewplane.js');"
            ),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "native Windows is not supported" in result.stderr
