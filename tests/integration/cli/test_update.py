from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from click import unstyle
from rich.console import Console
from typer.testing import CliRunner

import crewplane.cli.app as cli
from crewplane.cli.update import UpdateError


def test_help_lists_global_update_option() -> None:
    result = CliRunner().invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    output = unstyle(result.output)
    assert "--update" in output
    assert "-u" in output
    assert "Update Crewplane" in output


def test_cli_help_imports_without_packaging_runtime_dependency() -> None:
    root = Path(__file__).resolve().parents[3]
    environment = os.environ.copy()
    pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        os.pathsep.join([str(root / "src"), pythonpath])
        if pythonpath
        else str(root / "src")
    )
    script = textwrap.dedent(
        """
        import importlib.abc
        import sys

        class BlockPackaging(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "packaging" or fullname.startswith("packaging."):
                    raise ModuleNotFoundError("blocked packaging")
                return None

        sys.meta_path.insert(0, BlockPackaging())

        from click import unstyle
        from typer.testing import CliRunner
        from crewplane.cli import app as cli

        result = CliRunner().invoke(cli.app, ["--help"], catch_exceptions=False)
        if result.exit_code != 0:
            print(result.output)
            raise SystemExit(result.exit_code)
        if "--update" not in unstyle(result.output):
            raise SystemExit("missing --update")
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("option", ["--update", "-u"])
def test_global_update_option_is_eager_and_project_independent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    option: str,
) -> None:
    calls: list[Console] = []

    def fake_update(console: Console) -> int:
        calls.append(console)
        return 0

    monkeypatch.setattr(cli, "update_crewplane", fake_update)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli.app, [option])

    assert result.exit_code == 0
    assert not (tmp_path / ".crewplane").exists()
    assert len(calls) == 1


def test_global_update_option_renders_errors_as_literal_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_update(console: Console) -> int:
        assert isinstance(console, Console)
        raise UpdateError("[/oops] manual update guidance")

    monkeypatch.setattr(cli, "update_crewplane", fake_update)

    result = CliRunner().invoke(cli.app, ["--update"])

    assert result.exit_code == 1
    assert "[/oops] manual update guidance" in result.output


def test_global_update_option_preserves_package_manager_failure_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_update(console: Console) -> int:
        assert isinstance(console, Console)
        return 17

    monkeypatch.setattr(cli, "update_crewplane", fake_update)

    result = CliRunner().invoke(cli.app, ["-u"])

    assert result.exit_code == 17
