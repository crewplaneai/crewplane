from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from crewplane.cli.app import app
from tests.helpers.resume import make_run_manifest, write_run_manifest


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["--help"], ["runs", "cleanup"]),
        (["runs", "--help"], ["list", "show", "open", "logs"]),
        (["runs", "list", "--help"], ["recorded", "Elapsed"]),
        (["runs", "show", "--help"], ["RUN", "required"]),
        (["runs", "open", "--help"], ["RUN", "--results"]),
        (["runs", "logs", "--help"], ["RUN", "--node", "--follow"]),
    ],
)
def test_saved_run_help_exposes_only_inspection_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    expected: list[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, arguments, terminal_width=120)
    assert result.exit_code == 0, result.output
    for text in expected:
        assert text.lower() in result.output.lower()
    assert not (tmp_path / ".crewplane").exists()


@pytest.mark.parametrize("command", ["show", "open", "logs"])
def test_selecting_commands_require_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["runs", command])
    assert result.exit_code == 2
    assert "Missing argument" in result.output and "RUN" in result.output.upper()
    assert not (tmp_path / ".crewplane").exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["list", "--failed"],
        ["list", "--json"],
        ["show", "latest", "--results"],
        ["open", "latest", "--follow"],
        ["logs", "latest", "--results"],
    ],
)
def test_unsupported_options_fail_without_creating_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["runs", *arguments])
    assert result.exit_code == 2
    assert "No such option" in result.output
    assert not (tmp_path / ".crewplane").exists()


@pytest.mark.parametrize(
    "arguments", [["list"], ["show", "latest"], ["logs", "latest"], ["open", "latest"]]
)
def test_empty_project_never_initializes_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["runs", *arguments])
    assert result.exit_code == (0 if arguments == ["list"] else 1)
    assert not (tmp_path / ".crewplane").exists()


def _forbid_execution(monkeypatch: pytest.MonkeyPatch) -> list[Mock]:
    targets = [
        "crewplane.core.config.load_config",
        "crewplane.cli.workflow_context.load_config",
        "crewplane.cli.app.load_cli_workflow_context",
        "crewplane.cli.app.resolve_cli_workflow_paths",
        "crewplane.cli.workflow_context.load_workflow_source_for_preflight",
        "crewplane.bootstrap.container.build_runtime_components",
        "crewplane.cli.run.components.build_runtime_components",
        "crewplane.bootstrap.container.instantiate_adapter",
        "crewplane.architecture.loader.instantiate_adapter",
        "crewplane.adapters.invokers.cli.CliInvokerAdapter.create_invoker",
        "crewplane.artifacts.locks.acquire_same_context_lock",
        "crewplane.cli.run.execution.acquire_same_context_lock",
        "crewplane.artifacts.locks.posix_policy.PosixLockPolicy.recover_collision",
        "crewplane.artifacts.locks.manifest.finalize_stale_running_run",
        "crewplane.cli.run.historical_summary.refresh_historical_run_summary",
        "crewplane.cli.run.execution_helpers.refresh_historical_run_summary",
        "crewplane.core.state_paths.ensure_state_dir",
        "shutil.which",
        "subprocess.Popen",
    ]
    guards = []
    for target in targets:
        guard = Mock(side_effect=AssertionError(f"Inspection called {target}"))
        monkeypatch.setattr(target, guard)
        guards.append(guard)
    return guards


def _snapshot(root: Path) -> dict[str, tuple[bytes | None, int, int, int, int, int]]:
    snapshot = {}
    for path in (root, *root.rglob("*")):
        metadata = path.lstat()
        snapshot[path.relative_to(root).as_posix()] = (
            path.read_bytes() if path.is_file() else None,
            metadata.st_mode,
            metadata.st_ino,
            metadata.st_nlink,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )
    return snapshot


def _saved_project(root: Path, config: str, workflows: str) -> Path:
    state = root / ".crewplane"
    manifest = make_run_manifest("saved", "workflow-saved", "failed")
    path = write_run_manifest(state, manifest)
    logs = path.parents[1] / "logs"
    logs.mkdir()
    (logs / "summary.md").write_bytes(b"# Saved succeeded body sentinel\n")
    (logs / "events.ndjson").write_bytes(b"raw saved event evidence\n")
    (state / "execution-results" / manifest.run_key_name).mkdir(parents=True)
    lock = state / "locks" / "stale" / "owner.json"
    lock.parent.mkdir(parents=True)
    lock.write_bytes(b"stale lock sentinel")
    if config == "invalid":
        (state / "config.yml").write_bytes(b"agents: [invalid yaml\n")
    authored = state / "workflows"
    authored.mkdir()
    for name in ("workflow", "other"):
        workflow = authored / f"{name}.task.md"
        workflow.write_bytes(b"not a currently valid authored workflow")
        if workflows == "deleted":
            workflow.unlink()
    return state


@pytest.mark.parametrize("config", ["absent", "invalid"])
@pytest.mark.parametrize("workflows", ["multiple", "deleted"])
@pytest.mark.parametrize(
    "arguments",
    [
        ["list"],
        ["show", "saved"],
        ["open", "saved"],
        ["open", "saved", "--results"],
        ["logs", "saved"],
    ],
)
def test_inspection_is_read_only_and_independent_of_execution_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config: str,
    workflows: str,
    arguments: list[str],
) -> None:
    state = _saved_project(tmp_path, config, workflows)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "no-provider-executables"))
    guards = _forbid_execution(monkeypatch)
    launcher = Mock(return_value=0)
    monkeypatch.setattr("crewplane.cli.runs.reports.typer.launch", launcher)
    before = _snapshot(tmp_path)

    result = CliRunner().invoke(app, ["runs", *arguments], terminal_width=120)

    assert result.exit_code == 0, (result.output, result.exception)
    assert _snapshot(tmp_path) == before
    for guard in guards:
        guard.assert_not_called()
    if arguments[0] == "open":
        target = (
            state / "execution-results" / "workflow-saved"
            if "--results" in arguments
            else state / "execution-stages" / "workflow-saved" / "logs" / "summary.md"
        )
        launcher.assert_called_once_with(str(target.absolute()))
    else:
        launcher.assert_not_called()
    if arguments[0] == "show":
        assert "Recorded manifest outcome: failed" in result.output
        assert "Saved succeeded body sentinel" in result.output
    if arguments[0] == "logs":
        assert result.stdout == "raw saved event evidence\n"
