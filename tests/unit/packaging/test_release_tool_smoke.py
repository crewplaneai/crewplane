from __future__ import annotations

# ruff: noqa: E402, I001

import hashlib
from pathlib import Path
import sys

_LOCAL_TEST_DIR = Path(__file__).resolve().parent
if str(_LOCAL_TEST_DIR) not in sys.path:
    sys.path.insert(0, str(_LOCAL_TEST_DIR))

import pytest

from scripts.release import smoke, state
from test_release_tool_fixtures import write_minimal_repo


class BrewSmokeRunner:
    def __init__(self) -> None:
        self.installed_formula_text = ""

    def run(
        self,
        command,
        cwd: Path,
        env=None,
        timeout=None,
        capture_output: bool = True,
        check: bool = True,
    ) -> state.CommandResult:
        del cwd, env, timeout, capture_output, check
        command_tuple = tuple(command)
        if command_tuple[:3] == ("brew", "list", "--formula"):
            return state.CommandResult(command_tuple, 1, "", "")
        if command_tuple[:3] == ("brew", "install", "--build-from-source"):
            formula_path = Path(command_tuple[3])
            self.installed_formula_text = formula_path.read_text(encoding="utf-8")
        return state.CommandResult(command_tuple, 0, "", "")


def test_brew_smoke_uses_built_sdist_sha_for_local_formula(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_minimal_repo(tmp_path)
    context = state.read_release_context(tmp_path)
    sdist = tmp_path / "dist" / context.sdist_filename
    sdist.parent.mkdir()
    sdist_content = b"local sdist content"
    sdist.write_bytes(sdist_content)
    expected_sha = hashlib.sha256(sdist_content).hexdigest()

    def fake_package_build(root: Path, runner: BrewSmokeRunner) -> None:
        del root, runner

    monkeypatch.setattr(smoke, "command_exists", lambda name: name == "brew")
    monkeypatch.setattr(smoke.build, "package_build", fake_package_build)

    runner = BrewSmokeRunner()
    smoke.brew_smoke(tmp_path, runner)

    assert f'url "file://{sdist}"' in runner.installed_formula_text
    assert f'sha256 "{expected_sha}"' in runner.installed_formula_text
    assert f'sha256 "{"0" * 64}"' not in runner.installed_formula_text
