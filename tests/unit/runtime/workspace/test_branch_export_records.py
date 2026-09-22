from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from crewplane.artifacts import OutputManager
from crewplane.runtime.workspace.branch_export import records
from crewplane.runtime.workspace.branch_export.fulfillment import BranchExportCheckpoint
from crewplane.version import SCHEMA_VERSION
from tests.helpers.workspace_branch_export import branch_export_plan
from tests.helpers.workspace_service import create_git_repo


@pytest.mark.parametrize(
    "status",
    [
        "fulfilled",
        "skipped",
        "prepared",
        "failed_with_checkpoint",
        "failed_without_checkpoint",
    ],
)
@pytest.mark.parametrize("historical", [False, True])
def test_branch_export_records_preserve_complete_durable_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str, historical: bool
) -> None:
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, "feature/exported")
    policy = plan.nodes[0].workspace_policy
    assert policy is not None
    checkpoint = BranchExportCheckpoint(
        state_path=tmp_path / "state.json",
        state_relative_path="build/state.json",
        node_id="checkpoint-node",
        task_id="executor",
        result_commit="a" * 40,
        result_tree="b" * 40,
        result_ref="refs/crewplane/result",
        bundle_path=tmp_path / "result.bundle",
        bundle_relative_path="build/result.bundle",
        bundle_sha256="c" * 64,
        bundle_size_bytes=123,
    )
    fixed_time = datetime(2026, 9, 21, 12, 34, 56, tzinfo=UTC)
    clock = Mock()
    clock.now.return_value = fixed_time
    monkeypatch.setattr(records, "datetime", clock)
    run_id = "historical-run" if historical else plan.run_id
    run_key = "historical-key" if historical else plan.run_key_name
    origin = "verified_history" if historical else "current_run"
    identity = (plan, run_id, run_key, "primary")
    expected = {
        "version": SCHEMA_VERSION,
        "run_id": run_id,
        "run_key_name": run_key,
        "workflow_name": plan.workflow_name,
        "workflow_signature": plan.workflow_signature,
        "logical_worktree_name": "primary",
        "branch_name": "feature/exported",
        "branch_ref": "refs/heads/feature/exported",
        "operation_origin": origin,
        "recovery_mode": "initial",
        "repository_id": None,
        "dry_run": False,
        "created_at": "2026-09-21T12:34:56+00:00",
    }
    checkpoint_fields = {
        "node_id": "checkpoint-node",
        "task_id": "executor",
        "workspace_state_artifact": "build/state.json",
        "result_commit": "a" * 40,
        "result_tree": "b" * 40,
        "result_ref": "refs/crewplane/result",
        "bundle": {
            "path": "build/result.bundle",
            "sha256": "c" * 64,
            "size_bytes": 123,
        },
    }
    if status == "fulfilled":
        payload = records.branch_export_record(
            *identity,
            "feature/exported",
            "refs/heads/feature/exported",
            checkpoint,
            policy,
            "created",
            False,
            True,
            operation_origin=origin,
        )
        expected.update(
            status="fulfilled",
            operation="created",
            branch_exists_before=False,
            branch_exists_after=True,
            worktree_contract={"mode": "blob_exact", "schema_version": SCHEMA_VERSION},
            **checkpoint_fields,
        )
    elif status == "skipped":
        payload = records.skipped_branch_export_record(
            *identity, "request-node", operation_origin=origin
        )
        expected.update(
            status="skipped",
            operation="skipped",
            branch_name=None,
            branch_ref=None,
            branch_exists_before=None,
            branch_exists_after=None,
            skip_reason="create_branch_false",
            node_id="request-node",
        )
    elif status == "prepared":
        payload = records.prepared_branch_export_record(
            plan,
            run_id,
            run_key,
            "repository-id",
            "primary",
            "feature/exported",
            "refs/heads/feature/exported",
            checkpoint,
            policy,
            origin,
            "initial",
        )
        expected.update(
            status="prepared",
            operation="prepared",
            repository_id="repository-id",
            expected_old_oid=None,
            target_oid="a" * 40,
            worktree_contract={"mode": "blob_exact", "schema_version": SCHEMA_VERSION},
            **checkpoint_fields,
        )
    else:
        has_checkpoint = status == "failed_with_checkpoint"
        payload = records.failed_branch_export_record(
            *identity,
            "request-node",
            "feature/exported",
            "refs/heads/feature/exported",
            checkpoint if has_checkpoint else None,
            None,
            "verification failed",
            operation_origin=origin,
        )
        expected.update(
            status="failed_verification",
            operation="failed_verification",
            branch_exists_before=None,
            branch_exists_after=None,
            failure_message="verification failed",
            node_id="request-node",
        )
        if has_checkpoint:
            expected.update(checkpoint_fields)

    assert payload == expected
    clock.now.assert_called_once_with(UTC)
    output = OutputManager("exports", base_dir=tmp_path / "artifacts")
    record_path = output.write_workspace_export("primary", payload)
    assert (
        record_path.read_bytes()
        == (json.dumps(expected, indent=2, sort_keys=True) + "\n").encode()
    )
