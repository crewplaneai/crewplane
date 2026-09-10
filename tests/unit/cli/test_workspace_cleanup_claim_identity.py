from __future__ import annotations

import json
from pathlib import Path

import pytest

from crewplane.runtime.workspace.cleanup import AbsentWorkspaceStateProjection
from tests.unit.cli.workspace_cleanup_evidence_support import (
    REPOSITORY_ID,
    RUN_KEY,
    collect_cleanup_evidence,
    snapshot_cache_path,
    snapshot_claim_payload,
    worktree_claim_payload,
    write_cleanup_claim,
)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "different-run-id"),
        ("run_key_name", "different-run-key"),
        ("workflow_name", "different-workflow"),
        ("workflow_signature", "f" * 64),
        ("node_id", "different-node"),
    ],
)
def test_cleanup_evidence_rejects_claim_identity_that_conflicts_with_plan(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = snapshot_cache_path(cache_root)
    (workspace_path / "checkout").mkdir(parents=True)
    payload = snapshot_claim_payload(workspace_path)
    payload[field] = value
    write_cleanup_claim(stage_root, RUN_KEY, payload)

    evidence = collect_cleanup_evidence(stage_root, cache_root)
    decision = evidence.decision(RUN_KEY, workspace_path)

    assert decision.status == "invalid"
    assert decision.deletable is False
    assert evidence.ref_cleanup_run_keys() == ()


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        (None, "logical_worktree_name", "other"),
        (None, "clean_start", "tracked_only"),
        (None, "worktree_contract", {"mode": "other", "schema_version": "1"}),
        ("workspace", "materialization", "worktree_checkout"),
        ("workspace", "writable", False),
        ("workspace", "lineage_producer", True),
    ],
)
def test_cleanup_evidence_rejects_claim_that_conflicts_with_workspace_policy(
    tmp_path: Path,
    section: str | None,
    field: str,
    value: object,
) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = snapshot_cache_path(cache_root)
    (workspace_path / "checkout").mkdir(parents=True)
    payload = snapshot_claim_payload(workspace_path)
    target = payload if section is None else payload[section]
    assert isinstance(target, dict)
    target[field] = value
    write_cleanup_claim(stage_root, RUN_KEY, payload)

    decision = collect_cleanup_evidence(stage_root, cache_root).decision(
        RUN_KEY, workspace_path
    )

    assert decision.status == "invalid"
    assert decision.deletable is False


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("result", "lineage_produced", True),
        ("result", "lineage_discarded", False),
        ("result", "lineage_discard_reason", None),
        ("result", "lineage_discard_reason", ""),
        (None, "refs", {}),
        (None, "bundle", {}),
    ],
)
def test_cleanup_evidence_rejects_invalid_discarded_executor_claims(
    tmp_path: Path,
    section: str | None,
    field: str,
    value: object,
) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = cache_root / "workspaces" / REPOSITORY_ID / RUN_KEY / "executor"
    payload = worktree_claim_payload(workspace_path)
    payload["role"] = "executor"
    payload["result"] = {
        "lineage_produced": False,
        "lineage_discarded": True,
        "lineage_discard_reason": "unchanged_remediation",
    }
    target = payload if section is None else payload[section]
    assert isinstance(target, dict)
    target[field] = value
    write_cleanup_claim(stage_root, RUN_KEY, payload)

    evidence = collect_cleanup_evidence(stage_root, cache_root)
    decision = evidence.decision(RUN_KEY, workspace_path)

    assert decision.deletable is False
    assert decision.reason == "workspace evidence for the run is malformed"
    assert evidence.ref_cleanup_run_keys() == ()


def test_cleanup_evidence_rejects_claim_without_planned_workspace_policy(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = snapshot_cache_path(cache_root)
    (workspace_path / "checkout").mkdir(parents=True)
    write_cleanup_claim(stage_root, RUN_KEY, snapshot_claim_payload(workspace_path))
    plan_path = stage_root / RUN_KEY / "preflight" / "execution-plan.json"
    plan_payload = json.loads(plan_path.read_text(encoding="utf-8"))
    plan_payload["nodes"][0]["workspace_policy"] = None
    plan_path.write_text(json.dumps(plan_payload), encoding="utf-8")

    decision = collect_cleanup_evidence(stage_root, cache_root).decision(
        RUN_KEY, workspace_path
    )

    assert decision.status == "invalid"
    assert decision.deletable is False


@pytest.mark.parametrize("status", ["succeeded", "failed"])
@pytest.mark.parametrize("valid_identity", [False, True])
def test_cleanup_evidence_checks_hydrated_claim_identity_before_ignoring_path(
    tmp_path: Path,
    status: str,
    valid_identity: bool,
) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = snapshot_cache_path(cache_root)
    payload = snapshot_claim_payload(workspace_path)
    payload["status"] = status
    workspace = payload["workspace"]
    execution = payload["execution"]
    assert isinstance(workspace, dict)
    assert isinstance(execution, dict)
    payload["resume_origin"] = {
        "source_run_id": "source-run",
        "source_run_key_name": "source-run-key",
        "source_node_id": "build",
        "hydrated_at": "2026-08-30T00:00:00+00:00",
        "source_workspace": dict(workspace),
        "source_execution": dict(execution),
    }
    for field in ("path", "effective_cwd", "cache_root", "checkout_root", "cache_key"):
        workspace[field] = None
    workspace["retention"] = "not_applicable"
    workspace["retained_reason"] = "hydrated_resume"
    for field in (
        "cache_root",
        "workspace_path",
        "checkout_root",
        "effective_cwd",
        "worktree_git_dir",
    ):
        execution[field] = None
    if not valid_identity:
        payload["workflow_signature"] = "f" * 64
    write_cleanup_claim(stage_root, RUN_KEY, payload)

    decision = collect_cleanup_evidence(stage_root, cache_root).decision(
        RUN_KEY, workspace_path
    )

    assert decision.status == (None if valid_identity else "invalid")
    assert decision.deletable is valid_identity


def test_cleanup_evidence_rejects_reappeared_deleted_workspace(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = snapshot_cache_path(cache_root)
    checkout_root = workspace_path / "checkout"
    checkout_root.mkdir(parents=True)
    (checkout_root / "replacement.txt").write_text("replacement", encoding="utf-8")
    payload = snapshot_claim_payload(workspace_path)
    workspace = payload["workspace"]
    assert isinstance(workspace, dict)
    workspace["retention"] = "deleted"
    write_cleanup_claim(stage_root, RUN_KEY, payload)

    decision = collect_cleanup_evidence(stage_root, cache_root).decision(
        RUN_KEY, workspace_path
    )

    assert decision.status == "invalid"
    assert decision.deletable is False
    assert "reappeared after deletion" in str(decision.reason)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("workspace", "path", "/different/workspace"),
        ("workspace", "cache_key", "different-cache-key"),
        ("workspace", "effective_cwd", "/different/checkout"),
        ("execution", "cache_root", "/different/cache"),
        ("execution", "checkout_root", "/different/checkout"),
        ("execution", "effective_cwd", "/different/checkout"),
    ],
)
def test_cleanup_evidence_rejects_contradictory_placement_fields(
    tmp_path: Path,
    section: str,
    field: str,
    value: str,
) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = snapshot_cache_path(cache_root)
    (workspace_path / "checkout").mkdir(parents=True)
    payload = snapshot_claim_payload(workspace_path)
    placement = payload[section]
    assert isinstance(placement, dict)
    placement[field] = value
    write_cleanup_claim(stage_root, RUN_KEY, payload)

    decision = collect_cleanup_evidence(stage_root, cache_root).decision(
        RUN_KEY, workspace_path
    )

    assert decision.status == "invalid"
    assert decision.deletable is False


def test_cleanup_evidence_rejects_workspace_kind_cache_family_mismatch(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = cache_root / "workspaces" / REPOSITORY_ID / RUN_KEY / "snapshot"
    (workspace_path / "checkout").mkdir(parents=True)
    write_cleanup_claim(stage_root, RUN_KEY, snapshot_claim_payload(workspace_path))

    decision = collect_cleanup_evidence(stage_root, cache_root).decision(
        RUN_KEY, workspace_path
    )

    assert decision.status == "invalid"
    assert decision.deletable is False


def test_cleanup_evidence_projects_coherent_terminal_claim_when_path_is_absent(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "cache"
    stage_root = tmp_path / "stages"
    workspace_path = snapshot_cache_path(cache_root)
    state_path = write_cleanup_claim(
        stage_root,
        RUN_KEY,
        snapshot_claim_payload(workspace_path),
    )

    assert collect_cleanup_evidence(
        stage_root, cache_root
    ).absent_state_projections() == (
        AbsentWorkspaceStateProjection(
            RUN_KEY,
            workspace_path,
            "succeeded",
            (state_path,),
        ),
    )
