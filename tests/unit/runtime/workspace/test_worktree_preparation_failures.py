from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock

import pytest

from crewplane.core.preflight.models import WorkspaceSourceSnapshot
from crewplane.runtime.workspace.service import worktree_failures
from crewplane.runtime.workspace.state import read_workspace_state
from tests.helpers.resume import make_workspace_source_snapshot

type FailureRecorder = Callable[
    [WorkspaceSourceSnapshot, Path, Path | None, Exception], None
]

pytestmark = pytest.mark.parametrize(
    ("record_failure", "status"),
    [
        (worktree_failures.record_failed_worktree_preparation, "failed"),
        (worktree_failures.record_cancelled_worktree_preparation, "cancelled"),
    ],
)


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    path = tmp_path / "workspace-state.json"
    path.write_text(
        json.dumps(
            {
                "status": "running",
                "workspace": {"retention": "retained"},
                "execution": {"worktree_git_dir": str(tmp_path / "git-dir")},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_preparation_retains_workspace_when_terminal_publication_fails(
    monkeypatch: pytest.MonkeyPatch,
    state_path: Path,
    record_failure: FailureRecorder,
    status: str,
) -> None:
    monkeypatch.setattr(
        worktree_failures, "workspace_mutators_are_drained", Mock(return_value=True)
    )
    monkeypatch.setattr(
        worktree_failures,
        "update_workspace_state",
        Mock(side_effect=OSError("write denied")),
    )
    cleanup = Mock()
    monkeypatch.setattr(worktree_failures, "remove_worktree_workspace", cleanup)
    failure = RuntimeError("preparation stopped")

    record_failure(
        make_workspace_source_snapshot(), state_path.parent, state_path, failure
    )

    cleanup.assert_not_called()
    assert read_workspace_state(state_path)["status"] == "running"
    state_label = "failure-state" if status == "failed" else "cancelled-state"
    outcome = "failure" if status == "failed" else "cancellation"
    assert failure.__notes__ == [
        f"Workspace {state_label} recording after preparation {outcome} failed: "
        "write denied"
    ]


def test_preparation_records_retention_without_cleanup_when_drain_is_unresolved(
    monkeypatch: pytest.MonkeyPatch,
    state_path: Path,
    record_failure: FailureRecorder,
    status: str,
) -> None:
    monkeypatch.setattr(
        worktree_failures, "workspace_mutators_are_drained", Mock(return_value=False)
    )
    cleanup = Mock()
    monkeypatch.setattr(worktree_failures, "remove_worktree_workspace", cleanup)

    record_failure(
        make_workspace_source_snapshot(),
        state_path.parent,
        state_path,
        RuntimeError("preparation stopped"),
    )

    cleanup.assert_not_called()
    state = read_workspace_state(state_path)
    assert state["status"] == status
    assert state["workspace"] == {
        "retention": "retained",
        "retained_reason": "process_drain_unresolved",
    }


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_preparation_publishes_terminal_state_before_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    state_path: Path,
    record_failure: FailureRecorder,
    status: str,
    cleanup_fails: bool,
) -> None:
    monkeypatch.setattr(
        worktree_failures, "workspace_mutators_are_drained", Mock(return_value=True)
    )
    retained_reason = "preparation_failed" if status == "failed" else "cancelled"
    cleanup_calls: list[Path] = []

    def cleanup(source: WorkspaceSourceSnapshot, path: Path, git_dir: Path) -> None:
        assert source == make_workspace_source_snapshot()
        assert git_dir == state_path.parent / "git-dir"
        state = read_workspace_state(state_path)
        assert state["status"] == status
        assert state["workspace"] == {
            "retention": "pending_cleanup",
            "retained_reason": retained_reason,
        }
        cleanup_calls.append(path)
        if cleanup_fails:
            raise OSError("cleanup denied")

    monkeypatch.setattr(worktree_failures, "remove_worktree_workspace", cleanup)
    failure = RuntimeError("preparation stopped")

    record_failure(
        make_workspace_source_snapshot(), state_path.parent, state_path, failure
    )

    assert cleanup_calls == [state_path.parent]
    state = read_workspace_state(state_path)
    assert state["status"] == status
    if cleanup_fails:
        assert state["workspace"] == {
            "retention": "retained",
            "retained_reason": f"{retained_reason}_cleanup_failed",
        }
        outcome = "failure" if status == "failed" else "cancellation"
        message = (
            f"Workspace cleanup after preparation {outcome} failed: cleanup denied"
        )
        assert failure.__notes__ == [message]
        assert state["diagnostics"][-1] == {"level": "warning", "message": message}
    else:
        assert state["workspace"] == {"retention": "deleted", "retained_reason": None}
