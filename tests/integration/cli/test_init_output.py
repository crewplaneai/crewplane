from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

import crewplane.cli.app as cli


@pytest.mark.parametrize("encoding", ["ascii", "cp1252"])
def test_init_and_validate_support_restricted_output_encoding(
    tmp_path: Path,
    encoding: str,
) -> None:
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = f"{encoding}:strict"

    init_result = run_cli(tmp_path, environment, "init")

    assert init_result.returncode == 0, process_output(init_result, encoding)
    assert "\\u2713" in init_result.stdout.decode(encoding)
    assert (tmp_path / ".crewplane" / "config.yml").is_file()
    assert (
        tmp_path / ".crewplane" / "workflows" / "single-agent-review.task.md"
    ).is_file()
    assert (tmp_path / ".crewplane" / "preflight" / "fingerprint.key").is_file()

    validate_result = run_cli(tmp_path, environment, "validate")

    assert validate_result.returncode == 0, process_output(validate_result, encoding)
    assert "\\u2713" in validate_result.stdout.decode(encoding)


def test_init_warns_and_continues_on_native_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def native_windows() -> bool:
        return True

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "is_native_windows", native_windows)

    result = CliRunner().invoke(cli.app, ["init"])

    assert result.exit_code == 0, result.output
    assert "Native Windows is not officially supported" in result.output
    assert "Use WSL for a supported environment" in result.output
    assert (tmp_path / ".crewplane" / "config.yml").is_file()


def run_cli(
    working_directory: Path,
    environment: dict[str, str],
    command: str,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-m", "crewplane.cli.app", command],
        cwd=working_directory,
        env=environment,
        capture_output=True,
        check=False,
        timeout=30,
    )


def process_output(
    result: subprocess.CompletedProcess[bytes],
    encoding: str,
) -> str:
    return (result.stdout + result.stderr).decode(encoding, errors="replace")
