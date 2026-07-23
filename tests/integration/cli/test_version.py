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
    assert result.output == f"crewplane {distribution_version('crewplane')}\n"


def test_help_lists_global_version_option() -> None:
    result = CliRunner().invoke(cli.app, ["--help"])

    assert result.exit_code == 0, result.output

    output = unstyle(result.output)  # normalize the captured output

    assert "--version" in output
    assert re.search(r"(?<![\w-])-v(?![\w-])", output) is not None
    assert "Show the installed Crewplane package version" in output
    assert "and exit." in output
