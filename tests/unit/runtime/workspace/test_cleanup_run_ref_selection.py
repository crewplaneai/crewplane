from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import crewplane.runtime.workspace.cleanup as workspace_cleanup_module
from crewplane.runtime.workspace.cleanup import (
    AbsentWorkspaceStateProjection,
    WorkspaceCleanupEligibility,
    WorkspaceCleanupFilter,
    cleanup_workspace_cache,
)
from tests.unit.runtime.workspace.cleanup_support import (
    cache_workspace_path,
)


def test_cleanup_workspace_cache_preserves_multi_candidate_callback_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = cache_workspace_path(tmp_path, "snapshots", "run-1", "first")
    second = cache_workspace_path(tmp_path, "snapshots", "run-1", "second")
    first.mkdir(parents=True)
    second.mkdir()
    calls: list[str] = []

    def status_lookup(run_key: str, cache_key: str) -> str:
        assert run_key == "run-1"
        calls.append(f"status:{cache_key}")
        return "failed"

    def eligibility_lookup(
        run_key: str,
        workspace_path: Path,
        status: str | None,
    ) -> WorkspaceCleanupEligibility:
        assert run_key == "run-1"
        assert status == "failed"
        calls.append(f"eligibility:{workspace_path.name}")
        return WorkspaceCleanupEligibility(
            deletable=True,
            state_paths=(tmp_path / f"{workspace_path.name}.json",),
        )

    def matches_status(
        status: str | None,
        cleanup_filter: WorkspaceCleanupFilter,
    ) -> bool:
        del status, cleanup_filter
        calls.append("match")
        return True

    def disk_usage(workspace_path: Path) -> int:
        calls.append(f"size:{workspace_path.name}")
        return 1

    def remove_path(
        workspace_path: Path,
        expected_common_git_dir: Path | None,
        expected_worktree_git_dir: Path | None,
    ) -> None:
        del expected_common_git_dir, expected_worktree_git_dir
        calls.append(f"remove:{workspace_path.name}")

    def update_state(state_path: Path, retention: object) -> None:
        del retention
        calls.append(f"state:{state_path.stem}")

    def cleanup_refs(run_key: str) -> int:
        calls.append(f"refs:{run_key}")
        return 1

    monkeypatch.setattr(workspace_cleanup_module, "status_matches", matches_status)
    monkeypatch.setattr(workspace_cleanup_module, "worktree_disk_usage", disk_usage)
    monkeypatch.setattr(
        workspace_cleanup_module,
        "remove_unknown_workspace_path",
        remove_path,
    )
    monkeypatch.setattr(
        workspace_cleanup_module,
        "update_workspace_retention",
        update_state,
    )

    result = cleanup_workspace_cache(
        tmp_path,
        WorkspaceCleanupFilter(run_key_name="run-1"),
        dry_run=False,
        status_lookup=status_lookup,
        eligibility_lookup=eligibility_lookup,
        ref_cleanup=cleanup_refs,
    )

    assert [entry.path for entry in result.entries] == [first, second]
    assert calls == [
        "status:first",
        "eligibility:first",
        "match",
        "size:first",
        "remove:first",
        "state:first",
        "status:second",
        "eligibility:second",
        "match",
        "size:second",
        "remove:second",
        "state:second",
        "refs:run-1",
    ]


def test_cleanup_workspace_cache_deletes_refs_for_selected_absent_workspace(
    tmp_path: Path,
) -> None:
    deleted_runs: list[str] = []

    result = cleanup_workspace_cache(
        tmp_path,
        WorkspaceCleanupFilter(statuses=frozenset({"failed"})),
        dry_run=False,
        ref_cleanup=lambda run_key: deleted_runs.append(run_key) or 2,
        absent_state_projections=(
            ("run-1", tmp_path / "already-absent", "failed", ()),
        ),
    )

    assert deleted_runs == ["run-1"]
    assert result.removed_ref_count == 2


def test_cleanup_workspace_cache_preserves_refs_when_absent_workspace_run_remains(
    tmp_path: Path,
) -> None:
    retained = cache_workspace_path(tmp_path, "workspaces", "run-1", "retained")
    retained.mkdir(parents=True)
    deleted_runs: list[str] = []

    def succeeded_status(run_key: str, cache_key: str) -> str:
        del run_key, cache_key
        return "succeeded"

    result = cleanup_workspace_cache(
        tmp_path,
        WorkspaceCleanupFilter(statuses=frozenset({"failed"})),
        dry_run=False,
        status_lookup=succeeded_status,
        ref_cleanup=lambda run_key: deleted_runs.append(run_key) or 2,
        absent_state_projections=(
            AbsentWorkspaceStateProjection(
                "run-1",
                tmp_path / "already-absent",
                "failed",
                (),
            ),
        ),
    )

    assert retained.exists()
    assert deleted_runs == []
    assert result.removed_ref_count == 0


def test_cleanup_workspace_cache_preserves_refs_for_excluded_absent_workspace(
    tmp_path: Path,
) -> None:
    deleted_runs: list[str] = []

    result = cleanup_workspace_cache(
        tmp_path,
        WorkspaceCleanupFilter(statuses=frozenset({"failed"})),
        dry_run=False,
        ref_cleanup=lambda run_key: deleted_runs.append(run_key) or 2,
        absent_state_projections=(
            AbsentWorkspaceStateProjection(
                "run-1",
                tmp_path / "failed-absent",
                "failed",
                (),
            ),
            AbsentWorkspaceStateProjection(
                "run-1",
                tmp_path / "succeeded-absent",
                "succeeded",
                (),
            ),
        ),
    )

    assert deleted_runs == []
    assert result.removed_ref_count == 0


def test_cleanup_workspace_cache_does_not_age_select_absent_workspace(
    tmp_path: Path,
) -> None:
    deleted_runs: list[str] = []

    result = cleanup_workspace_cache(
        tmp_path,
        WorkspaceCleanupFilter(
            older_than_seconds=3600,
            statuses=frozenset({"failed"}),
        ),
        dry_run=False,
        ref_cleanup=lambda run_key: deleted_runs.append(run_key) or 2,
        absent_state_projections=(
            AbsentWorkspaceStateProjection(
                "run-1",
                tmp_path / "already-absent",
                "failed",
                (),
            ),
        ),
    )

    assert deleted_runs == []
    assert result.removed_ref_count == 0


@pytest.mark.parametrize("retained_by", ["age", "status", "eligibility"])
def test_cleanup_workspace_cache_preserves_refs_when_same_run_candidate_remains(
    tmp_path: Path,
    retained_by: str,
) -> None:
    removable = cache_workspace_path(tmp_path, "workspaces", "run-1", "removable")
    retained = cache_workspace_path(tmp_path, "snapshots", "run-1", "retained")
    removable.mkdir(parents=True)
    retained.mkdir(parents=True)
    old_time = time.time() - 7200
    os.utime(removable, (old_time, old_time))
    if retained_by != "age":
        os.utime(retained, (old_time, old_time))
    statuses = {
        "removable": "failed",
        "retained": "succeeded" if retained_by == "status" else "failed",
    }
    deleted_runs: list[str] = []

    def status_lookup(run_key: str, cache_key: str) -> str:
        assert run_key == "run-1"
        return statuses[cache_key]

    def eligibility_lookup(
        run_key: str,
        workspace_path: Path,
        status: str | None,
    ) -> WorkspaceCleanupEligibility:
        assert run_key == "run-1"
        cache_key = workspace_path.name
        assert status == statuses[cache_key]
        return WorkspaceCleanupEligibility(
            deletable=not (retained_by == "eligibility" and cache_key == "retained"),
            reason="workspace state is unverifiable",
        )

    result = cleanup_workspace_cache(
        tmp_path,
        WorkspaceCleanupFilter(
            run_key_name="run-1",
            older_than_seconds=3600,
            statuses=frozenset({"failed"}),
        ),
        dry_run=False,
        status_lookup=status_lookup,
        eligibility_lookup=eligibility_lookup,
        ref_cleanup=lambda run_key: deleted_runs.append(run_key) or 1,
    )

    assert not removable.exists()
    assert retained.exists()
    assert result.removed_count == 1
    assert result.removed_ref_count == 0
    assert deleted_runs == []
