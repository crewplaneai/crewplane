import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from crewplane.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSelectionRecord,
    WorkspaceSetupCommandRecord,
    WorkspaceSetupRecord,
)
from crewplane.runtime.workspace import setup as workspace_setup
from crewplane.runtime.workspace.setup import (
    WorkspaceSetupCancellation,
    WorkspaceSetupCancelled,
    WorkspaceSetupError,
    run_workspace_setup,
    workspace_setup_artifacts,
)
from crewplane.version import SCHEMA_VERSION
from tests.helpers.workspace_records import WORKTREE_CONTRACT


def test_profile_deadline_limits_later_commands_and_stops_after_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 100.0
    durations = iter([6.0, 4.0])
    wait_timeouts: list[float | None] = []

    def monotonic() -> float:
        return now

    class CompletedSetupProcess:
        pid = 123

        def wait(self, timeout: float | None = None) -> int:
            nonlocal now
            wait_timeouts.append(timeout)
            now += next(durations)
            return 0

        def poll(self) -> int:
            return 0

    popen = Mock(return_value=CompletedSetupProcess())
    monkeypatch.setattr(workspace_setup.subprocess, "Popen", popen)
    monkeypatch.setattr(workspace_setup.time, "monotonic", monotonic)
    monkeypatch.setattr(workspace_setup, "supports_posix_process_groups", lambda: False)
    state_path = tmp_path / "stage" / "workspace-state.json"

    with pytest.raises(WorkspaceSetupError, match="profile timed out") as caught:
        run_workspace_setup(
            _plan(10), _policy([["first"], ["second"], ["third"]]), tmp_path, state_path
        )

    assert wait_timeouts == [10.0, 4.0]
    assert [call.args[0] for call in popen.call_args_list] == [["first"], ["second"]]
    artifacts = workspace_setup_artifacts(state_path)
    metadata = json.loads(artifacts.metadata_path.read_text(encoding="utf-8"))
    assert metadata == caught.value.summary
    assert metadata["status"] == "timed_out"
    assert metadata["timed_out"] is True
    assert metadata["duration_seconds"] == 10.0
    assert [record["command_index"] for record in metadata["commands"]] == [0, 1]
    assert all(record["exit_code"] == 0 for record in metadata["commands"])
    assert "$ third" not in artifacts.log_path.read_text(encoding="utf-8")


def test_cancellation_after_final_command_is_persisted_before_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancellation = WorkspaceSetupCancellation()
    clear_process = cancellation.clear_process

    def cancel_after_command(process: subprocess.Popen[str]) -> None:
        clear_process(process)
        cancellation.cancel()

    monkeypatch.setattr(cancellation, "clear_process", cancel_after_command)
    state_path = tmp_path / "stage" / "workspace-state.json"

    with pytest.raises(
        WorkspaceSetupCancelled, match="profile was cancelled"
    ) as caught:
        run_workspace_setup(
            _plan(),
            _policy([[sys.executable, "-c", "print('completed command')"]]),
            tmp_path,
            state_path,
            cancellation=cancellation,
        )

    artifacts = workspace_setup_artifacts(state_path)
    metadata = json.loads(artifacts.metadata_path.read_text(encoding="utf-8"))
    assert metadata == caught.value.summary
    assert metadata["status"] == "cancelled"
    assert metadata["timed_out"] is False
    assert len(metadata["commands"]) == 1
    assert metadata["commands"][0]["exit_code"] == 0
    assert "cancelled" not in metadata["commands"][0]
    log_text = artifacts.log_path.read_text(encoding="utf-8")
    assert "[stdout]\ncompleted command\n" in log_text
    assert "[exit_code] 0" in log_text


def test_failed_command_stops_profile_and_preserves_output_before_raising(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "stage" / "workspace-state.json"
    commands = [
        [
            sys.executable,
            "-c",
            "import sys; print('before failure'); "
            "print('failure detail', file=sys.stderr); sys.exit(7)",
        ],
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('unexpected').write_text('ran')",
        ],
    ]

    with pytest.raises(WorkspaceSetupError, match="exit code 7") as caught:
        run_workspace_setup(_plan(), _policy(commands), tmp_path, state_path)

    assert not (tmp_path / "unexpected").exists()
    artifacts = workspace_setup_artifacts(state_path)
    metadata = json.loads(artifacts.metadata_path.read_text(encoding="utf-8"))
    assert metadata == caught.value.summary
    assert metadata["status"] == "failed"
    assert metadata["timed_out"] is False
    assert len(metadata["commands"]) == 1
    assert metadata["commands"][0]["exit_code"] == 7
    assert metadata["commands"][0]["argv"] == commands[0]
    log_text = artifacts.log_path.read_text(encoding="utf-8")
    assert "[stdout]\nbefore failure\n" in log_text
    assert "[stderr]\nfailure detail\n" in log_text
    assert "[exit_code] 7" in log_text


def test_spawn_error_is_recorded_and_stops_profile_before_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    popen = Mock(side_effect=OSError("setup executable unavailable"))
    monkeypatch.setattr(workspace_setup.subprocess, "Popen", popen)
    state_path = tmp_path / "stage" / "workspace-state-alpha.json"

    with pytest.raises(WorkspaceSetupError, match="exit code None") as caught:
        run_workspace_setup(
            _plan(), _policy([["unavailable"], ["later"]]), tmp_path, state_path
        )

    assert popen.call_count == 1
    artifacts = workspace_setup_artifacts(state_path)
    metadata = json.loads(artifacts.metadata_path.read_text(encoding="utf-8"))
    assert metadata == caught.value.summary
    assert metadata["status"] == "failed"
    assert metadata["timed_out"] is False
    assert len(metadata["commands"]) == 1
    record = metadata["commands"][0]
    assert record["argv"] == ["unavailable"]
    assert record["exit_code"] is None
    assert record["timed_out"] is False
    assert record["error"] == "setup executable unavailable"
    assert artifacts.log_path.read_text(encoding="utf-8") == (
        "$ unavailable\n[error] setup executable unavailable\n\n"
    )


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
        token_catalog=[],
        dependency_graph=[],
        runtime_config_snapshot={
            "schema_version": SCHEMA_VERSION,
            "workspace": {"setup_timeout_seconds": setup_timeout_seconds},
        },
        effective_runtime_config_signature="runtime-signature",
        fingerprint_metadata={"payload_version": "1"},
    )
