from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

import pytest

from crewplane.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSelectionRecord,
    WorkspaceSetupCommandRecord,
    WorkspaceSetupRecord,
)
from crewplane.core.preflight.secrets import SecretContext
from crewplane.runtime.agent.process import drain as process_drain
from crewplane.runtime.agent.process.drain import ProcessDrainError
from crewplane.runtime.workspace import setup as workspace_setup
from crewplane.runtime.workspace import state_evidence as workspace_state_evidence
from crewplane.runtime.workspace.mutator_fence import (
    fence_workspace_mutator,
    release_workspace_mutator,
    workspace_mutator_is_fenced,
)
from crewplane.runtime.workspace.setup import (
    WorkspaceSetupCancellation,
    WorkspaceSetupError,
    run_workspace_setup,
)
from crewplane.version import SCHEMA_VERSION
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


def test_workspace_setup_resolves_secret_argv_without_persisting_it(
    tmp_path: Path,
) -> None:
    secret = "WORKSPACE_SETUP_TEST_SECRET"
    handle = "config:workspace.setup_profiles.bootstrap.run.0.3"
    secret_context = SecretContext()
    secret_context.put(handle, secret)
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    state_path = tmp_path / "stage" / "workspace-state.json"
    state_path.parent.mkdir()
    policy = WorkspaceSelectionRecord(
        enabled=True,
        logical_worktree_name="primary",
        declaration_kind="worktree",
        materialization="worktree_checkout",
        worktree_contract=WORKTREE_CONTRACT,
        setup=WorkspaceSetupRecord(
            profile_name="bootstrap",
            commands=[
                WorkspaceSetupCommandRecord(
                    argv=[
                        sys.executable,
                        "-c",
                        (
                            "import pathlib, sys; "
                            "pathlib.Path('resolved.txt').write_text(sys.argv[1])"
                        ),
                        {"redacted": True, "value_handle": handle},
                    ],
                    command_index=0,
                )
            ],
        ),
        writable=True,
        lineage_producer=True,
    )

    run_workspace_setup(
        _plan(),
        policy,
        cwd,
        state_path,
        secret_context=secret_context,
    )

    assert (cwd / "resolved.txt").read_text(encoding="utf-8") == secret
    metadata = (state_path.parent / "workspace-setup" / "setup.json").read_text(
        encoding="utf-8"
    )
    setup_log = (state_path.parent / "workspace-setup" / "setup.log").read_text(
        encoding="utf-8"
    )
    assert secret not in metadata
    assert secret not in setup_log
    assert "<redacted>" in setup_log
    assert json.loads(metadata)["commands"][0]["argv"][3]["redacted"] is True


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name != "posix":
        pytest.skip("process-group cleanup is POSIX-only")
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    state_path = tmp_path / "stage" / "workspace-state.json"
    state_path.parent.mkdir()
    child_pid_path = tmp_path / "child.pid"
    child_script = (
        "import os, pathlib, time; "
        f"pid_path = pathlib.Path({str(child_pid_path)!r}); "
        "temporary_path = pid_path.with_suffix('.tmp'); "
        "temporary_path.write_text(str(os.getpid())); "
        "temporary_path.replace(pid_path); "
        "time.sleep(30)"
    )
    parent_script = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_script!r}]); "
        "time.sleep(10)"
    )
    policy = _policy([[sys.executable, "-c", parent_script]])
    original_popen = subprocess.Popen
    started_processes: list[subprocess.Popen[str]] = []

    def start_ready_setup_process(*args: Any, **kwargs: Any) -> subprocess.Popen[str]:
        process = original_popen(*args, **kwargs)
        started_processes.append(process)
        deadline = time.monotonic() + 5
        while not child_pid_path.exists():
            if process.poll() is not None:
                pytest.fail("Setup exited before its child reported readiness.")
            if time.monotonic() >= deadline:
                pytest.fail("Setup child did not report readiness within 5 seconds.")
            time.sleep(0.01)
        return process

    monkeypatch.setattr(workspace_setup.subprocess, "Popen", start_ready_setup_process)

    try:
        with pytest.raises(WorkspaceSetupError) as exc_info:
            run_workspace_setup(
                _plan(setup_timeout_seconds=0.2), policy, cwd, state_path
            )

        assert exc_info.value.summary["status"] == "timed_out"
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        for process in started_processes:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def test_run_workspace_setup_uses_controlled_git_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GIT_WORK_TREE", "/outside")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/tmp/hooks")
    monkeypatch.setenv("GIT_PROTOCOL_FROM_USER", "0")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "https")
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
                    "'GIT_CONFIG_GLOBAL': os.environ.get('GIT_CONFIG_GLOBAL'), "
                    "'GIT_PROTOCOL_FROM_USER': "
                    "os.environ.get('GIT_PROTOCOL_FROM_USER'), "
                    "'GIT_ALLOW_PROTOCOL': os.environ.get('GIT_ALLOW_PROTOCOL')"
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
    assert env_payload["GIT_PROTOCOL_FROM_USER"] == "0"
    assert env_payload["GIT_ALLOW_PROTOCOL"] == "https"


def test_run_workspace_setup_uses_process_group_capability(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    state_path = tmp_path / "stage" / "workspace-state.json"
    state_path.parent.mkdir()
    captured_start_new_session: list[bool] = []
    probed_process_groups: set[int] = set()

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

    def missing_process_group(process_group_id: int, signal_number: int) -> None:
        assert signal_number == 0
        probed_process_groups.add(process_group_id)
        raise ProcessLookupError

    monkeypatch.setattr(workspace_setup.subprocess, "Popen", fake_popen)
    # The fake PID must never probe or signal a real host process group.
    monkeypatch.setattr(workspace_setup.os, "killpg", missing_process_group)
    monkeypatch.setattr(
        workspace_setup,
        "supports_posix_process_groups",
        lambda: False,
    )

    run_workspace_setup(_plan(), _policy([["setup"]]), cwd, state_path)
    assert not probed_process_groups

    monkeypatch.setattr(
        workspace_setup,
        "supports_posix_process_groups",
        lambda: True,
    )
    run_workspace_setup(_plan(), _policy([["setup"]]), cwd, state_path)

    assert captured_start_new_session == [False, True]
    assert probed_process_groups == {SuccessfulSetupProcess.pid}


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
    tmp_path: Path,
) -> None:
    if not hasattr(signal, "SIGKILL"):
        pytest.skip("SIGKILL is unavailable on this platform")
    process = StubbornSetupProcess()
    cancellation = WorkspaceSetupCancellation()
    state_path = tmp_path / "workspace-state.json"
    state_path.write_text("{}", encoding="utf-8")
    fence_workspace_mutator(state_path)
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        workspace_setup,
        "supports_posix_process_groups",
        lambda: True,
    )

    def signal_group(pgid: int, termination_signal: signal.Signals | int) -> None:
        if termination_signal == 0:
            if process.stopped:
                raise ProcessLookupError
            return
        signals.append((pgid, cast(signal.Signals, termination_signal)))
        if termination_signal == signal.SIGKILL:
            process.stopped = True

    monkeypatch.setattr(workspace_setup.os, "killpg", signal_group)

    try:
        assert (
            cancellation.register_process(
                cast(subprocess.Popen[str], process),
                state_path,
            )
            is True
        )
        cancellation.cancel()

        assert signals == [(123, signal.SIGTERM), (123, signal.SIGKILL)]
        assert process.terminate_calls == 0
        assert process.kill_calls == 0
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        assert payload["process_drain"]["status"] == "confirmed"
        assert not workspace_mutator_is_fenced(state_path)
    finally:
        release_workspace_mutator(state_path)


def test_setup_permission_denial_records_unresolved_process_drain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = StubbornSetupProcess()
    cancellation = WorkspaceSetupCancellation()
    state_path = tmp_path / "workspace-state.json"
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        workspace_setup,
        "supports_posix_process_groups",
        lambda: True,
    )
    monkeypatch.setattr(process_drain, "PROCESS_GROUP_TERM_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(process_drain, "PROCESS_GROUP_KILL_GRACE_SECONDS", 0.0)

    def deny_signal(process_group_id: int, termination_signal: int) -> None:
        del process_group_id, termination_signal
        raise PermissionError

    monkeypatch.setattr(os, "killpg", deny_signal)

    assert (
        cancellation.register_process(
            cast(subprocess.Popen[str], process),
            state_path,
        )
        is True
    )
    with pytest.raises(ProcessDrainError):
        cancellation.cancel()

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["process_drain"]["status"] == "unresolved"
    assert workspace_mutator_is_fenced(state_path)
    release_workspace_mutator(state_path)


def test_setup_process_drain_write_failure_preserves_error_and_fence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = StubbornSetupProcess()
    cancellation = WorkspaceSetupCancellation()
    state_path = tmp_path / "workspace-state.json"
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        workspace_setup,
        "supports_posix_process_groups",
        lambda: True,
    )
    monkeypatch.setattr(process_drain, "PROCESS_GROUP_TERM_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(process_drain, "PROCESS_GROUP_KILL_GRACE_SECONDS", 0.0)

    def deny_signal(process_group_id: int, termination_signal: int) -> None:
        del process_group_id, termination_signal
        raise PermissionError

    def fail_process_drain_record(*args: object) -> None:
        del args
        raise OSError("transient state write failure")

    monkeypatch.setattr(os, "killpg", deny_signal)
    monkeypatch.setattr(
        workspace_state_evidence,
        "record_workspace_process_drain",
        fail_process_drain_record,
    )

    assert (
        cancellation.register_process(
            cast(subprocess.Popen[str], process),
            state_path,
        )
        is True
    )
    with pytest.raises(ProcessDrainError) as exc_info:
        cancellation.cancel()

    assert any(
        "Workspace process-drain evidence persistence failed: " in note
        for note in getattr(exc_info.value, "__notes__", ())
    )
    assert workspace_mutator_is_fenced(state_path)
    release_workspace_mutator(state_path)


class StubbornSetupProcess:
    pid = 123

    def __init__(self) -> None:
        self.terminate_calls = 0
        self.kill_calls = 0
        self.stopped = False

    def poll(self) -> int | None:
        return -9 if self.stopped else None

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1
        self.stopped = True

    def wait(self, timeout: float | None = None) -> int:
        if timeout is not None and not self.stopped:
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
        plan_schema_version=SCHEMA_VERSION,
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
