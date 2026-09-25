from __future__ import annotations

import json
from pathlib import Path

import pytest

from crewplane.runtime.workspace.branch_export.fulfillment import (
    BranchExportCheckpoint,
)
from crewplane.runtime.workspace.branch_export.reconciliation import (
    BranchExportReconciliation,
    BranchExportRecordExpectation,
    ConflictingPreparedBranchExport,
    classify_branch_export_record,
)
from crewplane.runtime.workspace.branch_export.records import (
    branch_export_record,
    prepared_branch_export_record,
    skipped_branch_export_record,
)
from tests.helpers.workspace_branch_export import branch_export_plan
from tests.helpers.workspace_service import create_git_repo


@pytest.mark.parametrize("origin", ("current_run", "verified_history"))
def test_classify_matching_prepared_branch_export_for_recovery(
    tmp_path: Path,
    origin: str,
) -> None:
    context = _context(tmp_path)
    payload = prepared_branch_export_record(
        context.plan,
        context.run_id,
        context.run_key_name,
        context.repository_id,
        context.logical_worktree_name,
        "feature/exported",
        context.branch_ref,
        context.checkpoint,
        context.policy,
        origin,
        "initial",
    )
    _write_record(context.record_path, payload)

    reconciliation = _classify(context)

    assert reconciliation.recovery_mode == "prepared_record"
    assert reconciliation.operation_origin == origin


def test_classify_rejects_conflicting_or_invalid_prepared_evidence(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    payload = prepared_branch_export_record(
        context.plan,
        context.run_id,
        context.run_key_name,
        context.repository_id,
        context.logical_worktree_name,
        "feature/exported",
        context.branch_ref,
        context.checkpoint,
        context.policy,
        "current_run",
        "initial",
    )
    payload["target_oid"] = "f" * 40
    _write_record(context.record_path, payload)

    with pytest.raises(ConflictingPreparedBranchExport, match="conflicting prepared"):
        _classify(context)

    payload["target_oid"] = context.checkpoint.result_commit
    payload["operation_origin"] = "unknown"
    _write_record(context.record_path, payload)
    with pytest.raises(ConflictingPreparedBranchExport, match="invalid origin"):
        _classify(context)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("branch_name", "other"),
        ("operation", "created"),
        ("recovery_mode", "unknown"),
        ("node_id", "other"),
        ("task_id", "other"),
        ("dry_run", True),
        ("bundle", {"path": "other", "sha256": "f" * 64, "size_bytes": 1}),
        ("worktree_contract", {}),
    ),
)
def test_classify_rejects_tampered_prepared_operational_evidence(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    context = _context(tmp_path)
    payload = prepared_branch_export_record(
        context.plan,
        context.run_id,
        context.run_key_name,
        context.repository_id,
        context.logical_worktree_name,
        "feature/exported",
        context.branch_ref,
        context.checkpoint,
        context.policy,
        "current_run",
        "initial",
    )
    payload[field] = value
    _write_record(context.record_path, payload)

    with pytest.raises(ConflictingPreparedBranchExport, match="conflicting prepared"):
        _classify(context)


def test_classify_returns_matching_fulfilled_record_idempotently(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    payload = branch_export_record(
        context.plan,
        context.run_id,
        context.run_key_name,
        context.logical_worktree_name,
        "feature/exported",
        context.branch_ref,
        context.checkpoint,
        context.policy,
        "created",
        False,
        True,
        repository_id=context.repository_id,
    )
    _write_record(context.record_path, payload)

    reconciliation = _classify(context)

    assert reconciliation.recovery_mode == "initial"
    assert reconciliation.idempotent_payload == payload


def test_classify_allows_well_formed_fulfilled_history_for_prior_branch(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    payload = branch_export_record(
        context.plan,
        context.run_id,
        context.run_key_name,
        context.logical_worktree_name,
        "feature/prior",
        "refs/heads/feature/prior",
        context.checkpoint,
        context.policy,
        "created",
        False,
        True,
        repository_id=context.repository_id,
    )
    _write_record(context.record_path, payload)

    reconciliation = _classify(context)

    assert reconciliation.idempotent_payload is None


def test_classify_rejects_current_run_initial_verified_existing(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    payload = branch_export_record(
        context.plan,
        context.run_id,
        context.run_key_name,
        context.logical_worktree_name,
        "feature/exported",
        context.branch_ref,
        context.checkpoint,
        context.policy,
        "verified_existing",
        True,
        True,
        repository_id=context.repository_id,
    )
    _write_record(context.record_path, payload)

    with pytest.raises(RuntimeError, match="terminal history contradicts"):
        _classify(context)


def test_classify_allows_replay_after_terminal_nonfulfilled_record(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    payload = skipped_branch_export_record(
        context.plan,
        context.run_id,
        context.run_key_name,
        context.logical_worktree_name,
        "implement",
        repository_id=context.repository_id,
    )
    _write_record(context.record_path, payload)

    reconciliation = _classify(context)

    assert reconciliation.recovery_mode == "initial"
    assert reconciliation.idempotent_payload is None


def test_classify_rejects_malformed_terminal_identity_and_status(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    payload = skipped_branch_export_record(
        context.plan,
        context.run_id,
        context.run_key_name,
        context.logical_worktree_name,
        "implement",
        repository_id=context.repository_id,
    )
    payload["workflow_signature"] = "other"
    _write_record(context.record_path, payload)
    with pytest.raises(RuntimeError, match="terminal history contradicts"):
        _classify(context)

    payload["status"] = "unknown"
    _write_record(context.record_path, payload)
    with pytest.raises(RuntimeError, match="record is malformed"):
        _classify(context)


def test_classify_rejects_unsafe_or_unreadable_record_files(tmp_path: Path) -> None:
    context = _context(tmp_path)
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    context.record_path.symlink_to(target)
    with pytest.raises(RuntimeError, match="missing or unsafe"):
        _classify(context)

    target.unlink()
    with pytest.raises(RuntimeError, match="missing or unsafe"):
        _classify(context)

    context.record_path.unlink()
    context.record_path.write_text("not-json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unreadable"):
        _classify(context)

    context.record_path.write_text("[]", encoding="utf-8")
    with pytest.raises(RuntimeError, match="invalid"):
        _classify(context)


class _Context:
    def __init__(self, tmp_path: Path) -> None:
        repo = create_git_repo(tmp_path)
        self.plan = branch_export_plan(repo, tmp_path, branch_name="feature/exported")
        source = self.plan.workspace_source
        assert source is not None
        policy = self.plan.nodes[0].workspace_policy
        assert policy is not None
        self.policy = policy
        self.run_id = "run"
        self.run_key_name = self.plan.run_key_name
        self.repository_id = source.repository_id
        self.logical_worktree_name = "primary"
        self.branch_ref = "refs/heads/feature/exported"
        self.record_path = tmp_path / "workspace-export.json"
        self.checkpoint = BranchExportCheckpoint(
            state_path=tmp_path / "workspace-state.json",
            state_relative_path="implement/workspace-state.json",
            node_id="implement",
            task_id="alpha",
            result_commit="c" * 40,
            result_tree="d" * 40,
            result_ref="refs/crewplane/runs/run/implement/result",
            bundle_path=tmp_path / "result.bundle",
            bundle_relative_path="implement/workspace-bundles/result.bundle",
            bundle_sha256="e" * 64,
            bundle_size_bytes=42,
        )


def _context(tmp_path: Path) -> _Context:
    return _Context(tmp_path)


def _classify(context: _Context) -> BranchExportReconciliation:
    return classify_branch_export_record(
        context.record_path,
        BranchExportRecordExpectation(
            plan=context.plan,
            run_id=context.run_id,
            run_key_name=context.run_key_name,
            repository_id=context.repository_id,
            logical_worktree_name=context.logical_worktree_name,
            branch_name="feature/exported",
            branch_ref=context.branch_ref,
            checkpoint=context.checkpoint,
            policy=context.policy,
        ),
    )


def _write_record(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


_CHECKPOINT_KEYS = (
    "workspace_state_artifact",
    "task_id",
    "result_commit",
    "result_tree",
    "result_ref",
    "bundle",
)


def _checkpoint_payload(context: _Context, status: str) -> dict[str, object]:
    payload = prepared_branch_export_record(
        context.plan,
        context.run_id,
        context.run_key_name,
        context.repository_id,
        context.logical_worktree_name,
        "feature/exported",
        context.branch_ref,
        context.checkpoint,
        context.policy,
        "current_run",
        "initial",
    )
    if status == "fulfilled":
        payload.update(
            status=status,
            operation="created",
            branch_exists_before=False,
            branch_exists_after=True,
        )
    elif status == "failed_verification":
        payload.update(
            status=status,
            operation=status,
            failure_message="failed",
            branch_exists_before=None,
            branch_exists_after=None,
        )
    elif status == "skipped":
        payload.update(
            status=status,
            operation=status,
            skip_reason="create_branch_false",
            branch_name=None,
            branch_ref=None,
            branch_exists_before=None,
            branch_exists_after=None,
        )
    return payload


@pytest.mark.parametrize("status", ["prepared", "fulfilled", "failed_verification"])
@pytest.mark.parametrize("field", ["node_id", *_CHECKPOINT_KEYS])
@pytest.mark.parametrize("remove", [False, True])
def test_checkpoint_field_removal_or_tampering_is_rejected(
    tmp_path: Path, status: str, field: str, remove: bool
) -> None:
    context = _context(tmp_path)
    payload = _checkpoint_payload(context, status)
    if remove:
        del payload[field]
    else:
        payload[field] = "tampered"
    _write_record(context.record_path, payload)

    error = ConflictingPreparedBranchExport if status == "prepared" else RuntimeError
    message = (
        "conflicting prepared"
        if status == "prepared"
        else "terminal history contradicts"
    )
    with pytest.raises(error, match=message):
        _classify(context)


@pytest.mark.parametrize("status", ["prepared", "fulfilled", "failed_verification"])
def test_extra_bundle_keys_are_not_accepted_as_matching_checkpoint(
    tmp_path: Path, status: str
) -> None:
    context = _context(tmp_path)
    payload = _checkpoint_payload(context, status)
    payload["bundle"]["extra"] = "unexpected"
    _write_record(context.record_path, payload)

    with pytest.raises(RuntimeError):
        _classify(context)


@pytest.mark.parametrize(
    "status", ["prepared", "fulfilled", "failed_verification", "skipped"]
)
@pytest.mark.parametrize("presence", ["absent", "task_only", "partial", "complete"])
@pytest.mark.parametrize("recovery_mode", ["initial", "prepared_record"])
def test_checkpoint_presence_preserves_status_specific_recovery_rules(
    tmp_path: Path, status: str, presence: str, recovery_mode: str
) -> None:
    context = _context(tmp_path)
    payload = _checkpoint_payload(context, status)
    payload["recovery_mode"] = recovery_mode
    if presence != "complete":
        keep = {"task_id"} if presence == "task_only" else set()
        if presence == "partial":
            keep = {"result_commit"}
        for field in _CHECKPOINT_KEYS:
            if field not in keep:
                del payload[field]
    _write_record(context.record_path, payload)
    accepted = (
        (status == "skipped" and recovery_mode == "initial")
        or (status == "fulfilled" and presence == "complete")
        or (
            status == "prepared"
            and presence == "complete"
            and recovery_mode == "initial"
        )
        or (
            status == "failed_verification"
            and (
                presence == "complete"
                or (presence == "absent" and recovery_mode == "initial")
            )
        )
    )
    if accepted:
        result = _classify(context)
        assert result.recovery_mode == (
            "prepared_record" if status == "prepared" else "initial"
        )
    else:
        with pytest.raises(RuntimeError):
            _classify(context)
