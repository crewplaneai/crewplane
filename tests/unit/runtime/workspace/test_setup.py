from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest

from orchestrator_cli.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSelectionRecord,
    WorkspaceSetupCommandRecord,
    WorkspaceSetupRecord,
)
from orchestrator_cli.runtime.workspace import setup as workspace_setup
from orchestrator_cli.runtime.workspace.setup import (
    WorkspaceSetupCancellation,
    WorkspaceSetupError,
    run_workspace_setup,
)
from orchestrator_cli.version import SCHEMA_VERSION
from tests.helpers.workspace_records import WORKTREE_CONTRACT


def test_run_workspace_setup_writes_success_metadata_and_log(tmp_path: Path) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    state_path = tmp_path / "stage" / "workspace-state.json"
    state_path.parent.mkdir()
    policy = _policy(
        [
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "from pathlib import Path; "
                    "Path('setup.txt').write_text('ok'); "
                    "print('setup stdout'); "
                    "print('setup stderr', file=sys.stderr)"
                ),
            ]
        ]
    )

    summary = run_workspace_setup(_plan(), policy, cwd, state_path)

    assert summary is not None
    assert summary["status"] == "succeeded"
    assert (cwd / "setup.txt").read_text(encoding="utf-8") == "ok"
    metadata_path = state_path.parent / "workspace-setup" / "setup.json"
    log_path = state_path.parent / "workspace-setup" / "setup.log"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["commands"][0]["exit_code"] == 0
    assert metadata["commands"][0]["working_directory"] == cwd.as_posix()
    log_text = log_path.read_text(encoding="utf-8")
    assert "$ " in log_text
    assert "[stdout]\nsetup stdout\n" in log_text
    assert "[stderr]\nsetup stderr\n" in log_text


def test_run_workspace_setup_raises_and_records_failed_command(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    state_path = tmp_path / "stage" / "workspace-state-alpha.json"
    state_path.parent.mkdir()
    policy = _policy([[sys.executable, "-c", "import sys; sys.exit(7)"]])

    with pytest.raises(WorkspaceSetupError) as exc_info:
        run_workspace_setup(_plan(), policy, cwd, state_path)

    summary = exc_info.value.summary
    assert summary["status"] == "failed"
    assert summary["commands"][0]["exit_code"] == 7
    assert summary["commands"][0]["working_directory"] == cwd.as_posix()
    metadata_path = state_path.parent / "workspace-setup" / "workspace-state-alpha.json"
    assert json.loads(metadata_path.read_text(encoding="utf-8"))["status"] == "failed"


def test_run_workspace_setup_timeout_terminates_child_process_group(
    tmp_path: Path,
) -> None:
    if os.name != "posix":
        pytest.skip("process-group cleanup is POSIX-only")
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    state_path = tmp_path / "stage" / "workspace-state.json"
    state_path.parent.mkdir()
    leaked_child_marker = tmp_path / "child-survived.txt"
    child_script = (
        "import pathlib, time; "
        "time.sleep(0.5); "
        f"pathlib.Path({str(leaked_child_marker)!r}).write_text('alive')"
    )
    parent_script = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_script!r}]); "
        "time.sleep(10)"
    )
    policy = _policy([[sys.executable, "-c", parent_script]])

    with pytest.raises(WorkspaceSetupError) as exc_info:
        run_workspace_setup(_plan(setup_timeout_seconds=0.2), policy, cwd, state_path)

    assert exc_info.value.summary["status"] == "timed_out"
    time.sleep(0.8)
    assert not leaked_child_marker.exists()


def test_run_workspace_setup_uses_controlled_git_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GIT_WORK_TREE", "/outside")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/tmp/hooks")
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    state_path = tmp_path / "stage" / "workspace-state.json"
    state_path.parent.mkdir()
    policy = _policy(
        [
            [
                sys.executable,
                "-c",
                (
                    "import json, os; "
                    "json.dump({"
                    "'GIT_WORK_TREE': os.environ.get('GIT_WORK_TREE'), "
                    "'GIT_CONFIG_COUNT': os.environ.get('GIT_CONFIG_COUNT'), "
                    "'GIT_CONFIG_NOSYSTEM': os.environ.get('GIT_CONFIG_NOSYSTEM'), "
                    "'GIT_CONFIG_GLOBAL': os.environ.get('GIT_CONFIG_GLOBAL')"
                    "}, open('env.json', 'w'))"
                ),
            ]
        ]
    )

    run_workspace_setup(_plan(), policy, cwd, state_path, cwd)

    env_payload = json.loads((cwd / "env.json").read_text(encoding="utf-8"))
    assert env_payload["GIT_WORK_TREE"] is None
    assert env_payload["GIT_CONFIG_COUNT"] != "1"
    assert env_payload["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env_payload["GIT_CONFIG_GLOBAL"] == os.devnull


def test_run_workspace_setup_uses_process_group_capability(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    state_path = tmp_path / "stage" / "workspace-state.json"
    state_path.parent.mkdir()
    captured_start_new_session: list[bool] = []

    class SuccessfulSetupProcess:
        pid = 123

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

        def poll(self) -> int:
            return 0

    def fake_popen(*args: object, **kwargs: Any) -> SuccessfulSetupProcess:
        del args
        captured_start_new_session.append(bool(kwargs["start_new_session"]))
        return SuccessfulSetupProcess()

    monkeypatch.setattr(workspace_setup.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        workspace_setup,
        "supports_posix_process_groups",
        lambda: False,
    )

    run_workspace_setup(_plan(), _policy([["setup"]]), cwd, state_path)

    monkeypatch.setattr(
        workspace_setup,
        "supports_posix_process_groups",
        lambda: True,
    )
    run_workspace_setup(_plan(), _policy([["setup"]]), cwd, state_path)

    assert captured_start_new_session == [False, True]


def test_setup_cancellation_uses_plain_process_termination_without_posix_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = StubbornSetupProcess()
    cancellation = WorkspaceSetupCancellation()
    monkeypatch.setattr(
        workspace_setup,
        "supports_posix_process_groups",
        lambda: False,
    )

    assert cancellation.register_process(cast(subprocess.Popen[str], process)) is True
    cancellation.cancel()

    assert process.terminate_calls == 1
    assert process.kill_calls == 1


def test_setup_cancellation_uses_process_group_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not hasattr(signal, "SIGKILL"):
        pytest.skip("SIGKILL is unavailable on this platform")
    process = StubbornSetupProcess()
    cancellation = WorkspaceSetupCancellation()
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        workspace_setup,
        "supports_posix_process_groups",
        lambda: True,
    )

    def fake_getpgid(pid: int) -> int:
        del pid
        return 456

    monkeypatch.setattr(workspace_setup.os, "getpgid", fake_getpgid)
    monkeypatch.setattr(
        workspace_setup.os,
        "killpg",
        lambda pgid, termination_signal: signals.append((pgid, termination_signal)),
    )

    assert cancellation.register_process(cast(subprocess.Popen[str], process)) is True
    cancellation.cancel()

    assert signals == [(456, signal.SIGTERM), (456, signal.SIGKILL)]
    assert process.terminate_calls == 0
    assert process.kill_calls == 0


class StubbornSetupProcess:
    pid = 123

    def __init__(self) -> None:
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1

    def wait(self, timeout: float | None = None) -> int:
        if timeout is not None:
            raise subprocess.TimeoutExpired("setup", timeout)
        return -9


def _policy(commands: list[list[str]]) -> WorkspaceSelectionRecord:
    return WorkspaceSelectionRecord(
        enabled=True,
        logical_worktree_name="primary",
        declaration_kind="worktree",
        materialization="worktree_checkout",
        worktree_contract=WORKTREE_CONTRACT,
        setup=WorkspaceSetupRecord(
            profile_name="bootstrap",
            commands=[
                WorkspaceSetupCommandRecord(argv=argv, command_index=index)
                for index, argv in enumerate(commands)
            ],
        ),
        writable=True,
        lineage_producer=True,
    )


def _plan(setup_timeout_seconds: float = 30.0) -> PreflightExecutionPlan:
    return PreflightExecutionPlan(
        run_id="run",
        run_key_name="run",
        project_root=".",
        context_root=".",
        manifest_root="./manifests",
        created_at="2026-06-16T00:00:00",
        workflow_name="workspace",
        workflow_signature="workflow-signature",
        execution_order=[],
        nodes=[],
        render_plans=[],
        static_resources=[],
        workspace_file_locators=[],
        token_catalog=[],
        dependency_graph=[],
        runtime_config_snapshot={
            "schema_version": SCHEMA_VERSION,
            "workspace": {"setup_timeout_seconds": setup_timeout_seconds},
        },
        effective_runtime_config_signature="runtime-signature",
        fingerprint_metadata={"payload_version": "1"},
    )
