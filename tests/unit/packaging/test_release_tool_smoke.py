from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from scripts.release import retry, smoke, state
from tests.helpers.platforms import requires_posix
from tests.unit.packaging.release_tool_support import write_minimal_repo


class BrewSmokeRunner:
    def __init__(
        self, tap_root: Path, fail_command: str | None = None, installed: bool = False
    ) -> None:
        self.tap_root = tap_root
        self.fail_command = fail_command
        self.installed = installed
        self.tap_name = ""
        self.persistent_developer_mode = False
        self.commands: list[tuple[str, ...]] = []
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
        del cwd, timeout, capture_output, check
        command_tuple = tuple(command)
        self.commands.append(command_tuple)
        if command_tuple[:3] == ("brew", "list", "--formula"):
            return state.CommandResult(
                command_tuple, 0 if self.installed else 1, "", ""
            )
        if command_tuple[:2] == ("brew", "tap-new"):
            if not (env or {}).get("HOMEBREW_DEVELOPER"):
                self.persistent_developer_mode = True
            self.tap_name = command_tuple[-1]
            (self.tap_root / "Formula").mkdir(parents=True)
        if command_tuple[:2] == ("brew", "--repository"):
            if self.fail_command == "--repository":
                raise state.ReleaseError("brew --repository failed")
            return state.CommandResult(command_tuple, 0, str(self.tap_root), "")
        if command_tuple[:3] == ("brew", "install", "--build-from-source"):
            if command_tuple[3].endswith(".rb"):
                raise state.ReleaseError("Homebrew requires formulae to be in a tap")
            assert command_tuple[3] == f"{self.tap_name}/crewplane"
            formula_path = self.tap_root / "Formula" / "crewplane.rb"
            self.installed_formula_text = formula_path.read_text(encoding="utf-8")
        if command_tuple[1] == self.fail_command:
            raise state.ReleaseError(f"brew {self.fail_command} failed")
        if command_tuple[:2] == ("brew", "untap"):
            shutil.rmtree(self.tap_root)
        return state.CommandResult(command_tuple, 0, "", "")


def test_installed_cli_smoke_invokes_shared_assertions(tmp_path: Path) -> None:
    from tests.unit.packaging.release_tool_support import FakeRunner

    write_minimal_repo(tmp_path)
    context = state.read_release_context(tmp_path)
    runner = FakeRunner()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    executable = tmp_path / "bin" / "crewplane"
    smoke.exercise_installed_cli(context, runner, executable, scratch)
    command = runner.commands[-1]
    assert "installed_package_assertions.py" in " ".join(command)
    assert command[command.index("--cli") + 1] == str(executable)
    assert command[command.index("--python") + 1] == str(executable.parent / "python")
    assert command[command.index("--expected-version") + 1] == context.version.project
    assert command[command.index("--checkout") + 1] == str(context.root)


@requires_posix
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

    runner = BrewSmokeRunner(tmp_path / "brew-tap")
    smoke.brew_smoke(tmp_path, runner)

    assert f'url "file://{sdist}"' in runner.installed_formula_text
    assert f'sha256 "{expected_sha}"' in runner.installed_formula_text
    assert f'sha256 "{"0" * 64}"' not in runner.installed_formula_text
    assert ("brew", "test", f"{runner.tap_name}/crewplane") in runner.commands
    assert runner.commands[-2:] == [
        ("brew", "uninstall", f"{runner.tap_name}/crewplane"),
        ("brew", "untap", runner.tap_name),
    ]
    assert not runner.tap_root.exists()
    assert not runner.persistent_developer_mode


@pytest.mark.parametrize("fail_command", ["--repository", "install", "test"])
@requires_posix
def test_brew_smoke_cleans_up_tap_after_failure(
    tmp_path: Path, fail_command: str
) -> None:
    write_minimal_repo(tmp_path)
    context = state.read_release_context(tmp_path)
    sdist = tmp_path / "dist" / context.sdist_filename
    sdist.parent.mkdir()
    sdist.write_bytes(b"local sdist content")
    runner = BrewSmokeRunner(tmp_path / "brew-tap", fail_command=fail_command)

    with pytest.raises(state.ReleaseError, match=f"brew {fail_command} failed"):
        smoke._brew_smoke(context, runner)

    assert runner.commands[-1] == ("brew", "untap", runner.tap_name)
    if fail_command != "--repository":
        assert runner.commands[-2] == (
            "brew",
            "uninstall",
            f"{runner.tap_name}/crewplane",
        )
    assert not runner.tap_root.exists()


def test_brew_smoke_preserves_an_existing_installation(tmp_path: Path) -> None:
    write_minimal_repo(tmp_path)
    context = state.read_release_context(tmp_path)
    runner = BrewSmokeRunner(tmp_path / "brew-tap", installed=True)

    smoke._brew_smoke(context, runner)

    assert runner.commands == [("brew", "list", "--formula", "crewplane")]
    assert not runner.tap_root.exists()


def test_post_publish_npm_check_retries_with_exponential_backoff(
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
        assert sleeps == [10, 2, 4][:calls]
        if calls < 3:
            raise state.ReleaseError("package is not visible yet")

    monkeypatch.setattr(smoke, "remote_npm_install_check", check)
    monkeypatch.setattr(retry.time, "sleep", sleeps.append)

    smoke.post_publish_npm_check(context, state.CommandRunner(), attempts=3)

    assert calls == 3
    assert sleeps == [10, 2, 4]


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
