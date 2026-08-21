from __future__ import annotations

import re
from importlib.metadata import version as distribution_version
from pathlib import Path

import pytest
from click import unstyle
from typer.testing import CliRunner

import crewplane.cli.app as cli


@pytest.mark.parametrize("option", ["--version", "-v"])
def test_global_version_option_prints_distribution_version(
    option: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli.app, [option])

    assert result.exit_code == 0, result.output
    package_name, _package_version = cli.installed_package_identity()
    assert result.output == f"{package_name} {distribution_version(package_name)}\n"


def test_help_lists_global_version_option() -> None:
    result = CliRunner().invoke(cli.app, ["--help"])

    assert result.exit_code == 0, result.output

    output = unstyle(result.output)  # normalize the captured output

    assert "--version" in output
    assert re.search(r"(?<![\w-])-v(?![\w-])", output) is not None
    assert "Show the installed Crewplane package version" in output
    assert "and exit." in output


def test_global_version_option_reports_missing_distribution_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = (
        "The installed Crewplane package metadata could not be found. "
        "Reinstall Crewplane with the package manager that owns this command."
    )

    def missing_package_identity() -> tuple[str, str]:
        raise cli.UpdateError(message)

    monkeypatch.setattr(cli, "installed_package_identity", missing_package_identity)

    result = CliRunner().invoke(cli.app, ["--version"])

    assert result.exit_code == 1
    assert result.output == f"{message}\n"
    assert not isinstance(result.exception, cli.UpdateError)
