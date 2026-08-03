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


class InstalledCliRecordingRunner:
    def __init__(self, version: str) -> None:
        self.version = version
        self.calls: list[tuple[tuple[str, ...], Path]] = []

    def run(
        self,
        command,
        cwd: Path,
        env=None,
        timeout=None,
        capture_output: bool = True,
        check: bool = True,
    ) -> state.CommandResult:
        del env, timeout, capture_output, check
        command_tuple = tuple(command)
        self.calls.append((command_tuple, cwd))
        if command_tuple[-1:] == ("init",):
            (cwd / ".crewplane").mkdir()
        stdout = (
            f"crewplane {self.version}\n"
            if command_tuple[-1:] == ("--version",)
            else ""
        )
        return state.CommandResult(command_tuple, 0, stdout, "")


def test_installed_cli_smoke_exercises_a_workflow(tmp_path: Path) -> None:
    write_minimal_repo(tmp_path)
    context = state.read_release_context(tmp_path)
    runner = InstalledCliRecordingRunner(context.version.project)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    executable = tmp_path / "bin" / "crewplane"

    smoke.exercise_installed_cli(context, runner, executable, scratch)

    project = scratch / "project"
    assert [call for call, cwd in runner.calls if cwd == project] == [
        (str(executable), "init"),
        (str(executable), "validate"),
        (str(executable), "run", "--no-live"),
    ]


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


def test_post_publish_npm_check_retries_after_two_seconds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_repo(tmp_path)
    context = state.read_release_context(tmp_path)
    calls = 0
    sleeps: list[int] = []

    def check(
        context_arg: state.ReleaseContext,
        runner_arg: state.CommandRunner,
    ) -> None:
        nonlocal calls
        del context_arg, runner_arg
        calls += 1
        if calls < 3:
            raise state.ReleaseError("package is not visible yet")

    monkeypatch.setattr(smoke, "remote_npm_install_check", check)
    monkeypatch.setattr(smoke.time, "sleep", lambda seconds: sleeps.append(seconds))

    smoke.post_publish_npm_check(context, state.CommandRunner(), attempts=3)

    assert calls == 3
    assert sleeps == [2, 4]


def test_install_check_prepares_each_artifact_set_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_minimal_repo(tmp_path)
    build_steps: list[str] = []
    smoke_steps: list[str] = []

    def record_step(target: list[str], step: str):
        def record(*args) -> None:
            del args
            target.append(step)

        return record

    monkeypatch.setattr(
        smoke.build,
        "package_check",
        record_step(build_steps, "python"),
    )
    monkeypatch.setattr(
        smoke.build,
        "build_wheelhouse",
        record_step(build_steps, "wheelhouse"),
    )
    monkeypatch.setattr(
        smoke.build,
        "npm_pack",
        record_step(build_steps, "npm"),
    )
    monkeypatch.setattr(smoke, "command_exists", bool)
    monkeypatch.setattr(
        smoke,
        "_install_smoke_pip",
        record_step(smoke_steps, "pip"),
    )
    monkeypatch.setattr(
        smoke,
        "_install_smoke_uv",
        record_step(smoke_steps, "uv"),
    )
    monkeypatch.setattr(
        smoke,
        "_install_smoke_pipx",
        record_step(smoke_steps, "pipx"),
    )
    monkeypatch.setattr(
        smoke,
        "_install_script_smoke",
        record_step(smoke_steps, "install-script"),
    )
    monkeypatch.setattr(
        smoke,
        "_npm_smoke",
        record_step(smoke_steps, "npm"),
    )
    monkeypatch.setattr(
        smoke,
        "_brew_smoke",
        record_step(smoke_steps, "brew"),
    )

    smoke.install_check(tmp_path, state.CommandRunner())

    assert build_steps == ["python", "wheelhouse", "npm"]
    assert smoke_steps == ["pip", "uv", "pipx", "install-script", "npm", "brew"]
