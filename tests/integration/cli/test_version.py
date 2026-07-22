from __future__ import annotations

from importlib.metadata import version as distribution_version

import pytest
from typer.testing import CliRunner

import crewplane.cli.app as cli


@pytest.mark.parametrize("option", ["--version", "-v"])
def test_global_version_option_prints_distribution_version(option: str) -> None:
    runner = CliRunner()

    with runner.isolated_filesystem():
        result = runner.invoke(cli.app, [option])

    assert result.exit_code == 0
    assert result.output == f"crewplane {distribution_version('crewplane')}\n"


def test_help_lists_global_version_option() -> None:
    result = CliRunner().invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    assert "--version" in result.output
    assert "-v" in result.output
    assert "Show the installed Crewplane package version" in result.output
    assert "and exit." in result.output
