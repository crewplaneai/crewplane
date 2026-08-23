from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.release import build, smoke, state
from tests.unit.packaging.release_tool_support import (
    FakeRunner,
    constant,
    no_op,
    write_minimal_repo,
)


@dataclass(frozen=True)
class RecordedCommand:
    command: tuple[str, ...]
    cwd: Path
    env: dict[str, str] | None
    timeout: int | None
    capture_output: bool
    check: bool


@dataclass(frozen=True)
class InstalledCliInvocation:
    context: state.ReleaseContext
    runner: object
    executable: Path
    temporary: Path
    env: dict[str, str] | None


class ArtifactWritingRunner(FakeRunner):
    def __init__(self, context: state.ReleaseContext) -> None:
        super().__init__()
        self.context = context

    def run(
        self,
        command,
        cwd: Path,
        env=None,
        timeout=None,
        capture_output: bool = True,
        check: bool = True,
    ) -> state.CommandResult:
        result = super().run(command, cwd, env, timeout, capture_output, check)
        command_tuple = tuple(command)
        if command_tuple[1:3] == ("-m", "build"):
            dist = self.context.root / "dist"
            dist.mkdir(exist_ok=True)
            (dist / self.context.sdist_filename).write_bytes(b"sdist")
            (dist / self.context.wheel_filename).write_bytes(b"wheel")
        if command_tuple[:2] == ("npm", "pack"):
            package = self.context.root / ".release" / "npm" / self.context.npm_filename
            package.write_bytes(b"npm")
        return result


class SmokeRecordingRunner:
    def __init__(self) -> None:
        self.calls: list[RecordedCommand] = []
        self.python = "/opt/crewplane-smoke/python"
        self.uv_bin = "/opt/crewplane-smoke/uv-bin"

    def run(
        self,
        command: Sequence[str],
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout: int | None = state.COMMAND_TIMEOUT_SECONDS,
        capture_output: bool = True,
        check: bool = True,
    ) -> state.CommandResult:
        command_tuple = tuple(command)
        self.calls.append(
            RecordedCommand(
                command_tuple,
                cwd,
                dict(env) if env is not None else None,
                timeout,
                capture_output,
                check,
            )
        )
        stdout = ""
        if command_tuple == current_python_command():
            stdout = f"{self.python}\n"
        elif command_tuple == ("uv", "tool", "dir", "--bin"):
            stdout = f"{self.uv_bin}\n"
        return state.CommandResult(command_tuple, 0, stdout, "")


class InstalledCliRecorder:
    def __init__(self) -> None:
        self.calls: list[InstalledCliInvocation] = []

    def __call__(
        self,
        context: state.ReleaseContext,
        runner: object,
        executable: Path,
        temporary: Path,
        env: dict[str, str] | None = None,
    ) -> None:
        self.calls.append(
            InstalledCliInvocation(context, runner, executable, temporary, env)
        )


@pytest.fixture
def installed_cli_recorder(
    monkeypatch: pytest.MonkeyPatch,
) -> InstalledCliRecorder:
    recorder = InstalledCliRecorder()
    monkeypatch.setattr(smoke, "exercise_installed_cli", recorder)
    return recorder


def test_package_wheelhouse_builds_then_downloads_offline_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_repo(tmp_path)
    context = state.read_release_context(tmp_path)
    runner = ArtifactWritingRunner(context)
    monkeypatch.setattr(build, "command_exists", constant(False))

    build.package_wheelhouse(tmp_path, runner)

    wheelhouse = tmp_path / ".release" / "wheelhouse"
    wheel = tmp_path / "dist" / context.wheel_filename
    assert runner.commands == [
        python_build_command(),
        (
            sys.executable,
            "-m",
            "pip",
            "download",
            "--dest",
            str(wheelhouse),
            str(wheel),
        ),
    ]
    assert wheelhouse.is_dir()


def test_npm_pack_writes_tarball_with_expected_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_repo(tmp_path)
    context = state.read_release_context(tmp_path)
    runner = ArtifactWritingRunner(context)
    monkeypatch.setattr(build, "fail_if_generated_metadata_stale", no_op)
    monkeypatch.setattr(build, "command_exists", constant(True))

    build.npm_pack(tmp_path, runner)

    package_dir = tmp_path / ".release" / "npm"
    assert runner.commands == [
        (
            "npm",
            "pack",
            "./packaging/npm",
            "--pack-destination",
            str(package_dir),
        )
    ]
    assert (package_dir / context.npm_filename).read_bytes() == b"npm"


def test_package_check_runs_build_and_twine_for_matching_formula_sha(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_repo(tmp_path)
    context = state.read_release_context(tmp_path)
    runner = ArtifactWritingRunner(context)
    formula = tmp_path / "packaging" / "homebrew" / "Formula" / "crewplane.rb"
    expected_sha = hashlib.sha256(b"sdist").hexdigest()
    formula.write_text(
        formula.read_text(encoding="utf-8").replace("0" * 64, expected_sha, 1),
        encoding="utf-8",
    )
    monkeypatch.setattr(build, "fail_if_generated_metadata_stale", no_op)

    build.package_check(tmp_path, runner)

    assert runner.commands == [
        python_build_command(),
        (
            sys.executable,
            "-m",
            "twine",
            "check",
            str(tmp_path / "dist" / context.sdist_filename),
            str(tmp_path / "dist" / context.wheel_filename),
        ),
    ]


def test_pip_smoke_creates_venv_installs_offline_and_hands_off_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    installed_cli_recorder: InstalledCliRecorder,
) -> None:
    context = release_context(tmp_path)
    temporary = fixed_temporary_directory(tmp_path, "pip-smoke", monkeypatch)
    monkeypatch.setattr(smoke, "command_exists", constant(False))
    runner = SmokeRecordingRunner()

    smoke._install_smoke_pip(context, runner)

    venv = temporary / "venv"
    assert runner.calls == [
        command_call(current_python_command(), tmp_path),
        command_call((sys.executable, "-m", "venv", str(venv)), tmp_path),
        command_call(
            (
                str(venv / "bin" / "python"),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                str(smoke.wheelhouse(context)),
                f"crewplane=={context.version.project}",
            ),
            tmp_path,
        ),
    ]
    assert installed_cli_recorder.calls == [
        InstalledCliInvocation(
            context,
            runner,
            venv / "bin" / "crewplane",
            temporary,
            None,
        )
    ]


def test_uv_smoke_uses_isolated_home_and_hands_off_installed_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    installed_cli_recorder: InstalledCliRecorder,
) -> None:
    context = release_context(tmp_path)
    temporary = fixed_temporary_directory(tmp_path, "uv-smoke", monkeypatch)
    runner = SmokeRecordingRunner()
    env = {"HOME": str(temporary / "home")}

    smoke._install_smoke_uv(context, runner)

    assert runner.calls == [
        command_call(current_python_command(), tmp_path),
        command_call(
            (
                "uv",
                "tool",
                "install",
                "--force",
                "--python",
                runner.python,
                "--find-links",
                str(smoke.wheelhouse(context)),
                "--no-index",
                f"crewplane=={context.version.project}",
            ),
            tmp_path,
            env,
        ),
        command_call(("uv", "tool", "dir", "--bin"), tmp_path, env),
    ]
    assert installed_cli_recorder.calls == [
        InstalledCliInvocation(
            context,
            runner,
            Path(runner.uv_bin) / "crewplane",
            temporary,
            None,
        )
    ]


def test_pipx_smoke_uses_isolated_directories_and_hands_off_installed_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    installed_cli_recorder: InstalledCliRecorder,
) -> None:
    context = release_context(tmp_path)
    temporary = fixed_temporary_directory(tmp_path, "pipx-smoke", monkeypatch)
    runner = SmokeRecordingRunner()
    env = {
        "PIPX_HOME": str(temporary / "pipx-home"),
        "PIPX_BIN_DIR": str(temporary / "bin"),
    }

    smoke._install_smoke_pipx(context, runner)

    assert runner.calls == [
        command_call(current_python_command(), tmp_path),
        command_call(
            (
                "pipx",
                "install",
                "--force",
                "--python",
                runner.python,
                f"--pip-args=--no-index --find-links {smoke.wheelhouse(context)}",
                f"crewplane=={context.version.project}",
            ),
            tmp_path,
            env,
        ),
    ]
    assert installed_cli_recorder.calls == [
        InstalledCliInvocation(
            context,
            runner,
            temporary / "bin" / "crewplane",
            temporary,
            None,
        )
    ]


def test_install_script_smoke_passes_complete_offline_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = release_context(tmp_path)
    temporary = fixed_temporary_directory(tmp_path, "script-smoke", monkeypatch)
    runner = SmokeRecordingRunner()
    home = temporary / "home"
    env = {
        "CREWPLANE_VERSION": context.version.project,
        "CREWPLANE_INSTALL_FIND_LINKS": str(smoke.wheelhouse(context)),
        "CREWPLANE_INSTALL_NO_INDEX": "1",
        "CREWPLANE_INSTALL_PYTHON": runner.python,
        "CREWPLANE_INSTALL_HOME": str(home),
        "HOME": str(home),
    }

    smoke._install_script_smoke(context, runner)

    assert runner.calls == [
        command_call(current_python_command(), tmp_path),
        command_call(("sh", "install.sh"), tmp_path, env),
    ]


def test_npm_smoke_installs_tarball_offline_and_hands_off_installed_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    installed_cli_recorder: InstalledCliRecorder,
) -> None:
    context = release_context(tmp_path)
    package = tmp_path / ".release" / "npm" / context.npm_filename
    package.parent.mkdir(parents=True)
    package.write_bytes(b"npm")
    temporary = fixed_temporary_directory(tmp_path, "npm-smoke", monkeypatch)
    runner = SmokeRecordingRunner()
    prefix = temporary / "prefix"
    base_env = {
        "HOME": str(temporary / "home"),
        "NPM_CONFIG_CACHE": str(temporary / "npm-cache"),
        "XDG_CACHE_HOME": str(temporary / "xdg-cache"),
        "CREWPLANE_VERSION": context.version.project,
        "CREWPLANE_INSTALL_FIND_LINKS": str(smoke.wheelhouse(context)),
        "CREWPLANE_INSTALL_NO_INDEX": "1",
        "CREWPLANE_INSTALL_PYTHON": runner.python,
    }

    smoke._npm_smoke(context, runner)

    assert runner.calls == [
        command_call(current_python_command(), tmp_path),
        command_call(
            (
                "npm",
                "install",
                "-g",
                str(package),
                "--prefix",
                str(prefix),
                "--foreground-scripts",
            ),
            tmp_path,
            base_env,
        ),
    ]
    installed_env = {
        "PATH": f"{prefix / 'bin'}:{os.environ.get('PATH', '')}",
        **base_env,
    }
    assert installed_cli_recorder.calls == [
        InstalledCliInvocation(
            context,
            runner,
            prefix / "bin" / "crewplane",
            temporary,
            installed_env,
        )
    ]


def release_context(root: Path) -> state.ReleaseContext:
    write_minimal_repo(root)
    return state.read_release_context(root)


def fixed_temporary_directory(
    root: Path,
    name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    path = root / name
    path.mkdir()
    monkeypatch.setattr(
        smoke.tempfile,
        "TemporaryDirectory",
        constant(nullcontext(str(path))),
    )
    return path


def command_call(
    command: tuple[str, ...],
    cwd: Path,
    env: dict[str, str] | None = None,
) -> RecordedCommand:
    return RecordedCommand(
        command,
        cwd,
        env,
        state.COMMAND_TIMEOUT_SECONDS,
        True,
        True,
    )


def current_python_command() -> tuple[str, ...]:
    return (sys.executable, "-c", "import sys; print(sys.executable)")


def python_build_command() -> tuple[str, ...]:
    return (
        sys.executable,
        "-m",
        "build",
        "--sdist",
        "--wheel",
        "--outdir",
        "dist",
    )
