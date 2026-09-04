from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from crewplane.artifacts import OutputManager
from crewplane.artifacts.naming import build_workspace_export_filename
from crewplane.core.preflight.models import WorkspaceSourceSnapshot
from crewplane.runtime.workspace.branch_export import attempts as branch_export_attempts
from crewplane.runtime.workspace.branch_export import (
    fulfill_branch_exports,
    fulfill_branch_exports_from_history,
    preview_branch_exports_from_history,
)
from crewplane.runtime.workspace.branch_export import (
    fulfillment as branch_export_fulfillment,
)
from crewplane.runtime.workspace.branch_export import git as branch_export_git
from crewplane.runtime.workspace.branch_export.fulfillment import (
    BranchExportCheckpoint,
)
from crewplane.runtime.workspace.branch_export.git import BranchExportOperation
from crewplane.runtime.workspace.branch_export.records import (
    checkpoint_from_record,
    prepared_branch_export_record,
    skipped_branch_export_record,
)
from crewplane.runtime.workspace.worktree.types import WorktreeSourceRef
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.workspace_branch_export import (
    branch_export_plan,
    history_record_for_output,
    write_node_manifest,
    write_result_bundle,
    write_tree_bundle,
    write_workspace_state,
)
from tests.helpers.workspace_lineage_bundles import create_prerequisite_bundle_chain
from tests.helpers.workspace_service import (
    create_git_repo,
    git_commit_exists,
    run_git_text,
)


def test_preview_branch_exports_rejects_result_ref_that_is_not_a_commit(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/preview")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_tree, result_ref, bundle_path = write_tree_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        result_tree,
        result_tree,
        result_ref,
        bundle_path,
    )
    history = history_record_for_output(output)

    records = preview_branch_exports_from_history(plan, history)

    assert len(records) == 1
    record = records[0]
    assert record["dry_run"] is True
    assert record["status"] == "failed_verification"
    assert "recorded commit and tree" in record["failure_message"]
    assert run_git_text(repo, "branch", "--list", "feature/preview") == ""


def test_checkpoint_from_record_rejects_malformed_required_fields(
    tmp_path: Path,
) -> None:
    payload = {
        "workspace_state_artifact": "implement/workspace-state.json",
        "node_id": 123,
        "task_id": "alpha",
        "result_commit": "a" * 40,
        "result_tree": "b" * 40,
        "result_ref": "refs/crewplane/run/implement/result",
        "bundle": {
            "path": "implement/workspace-bundles/result.bundle",
            "sha256": "c" * 64,
            "size_bytes": "12",
        },
    }

    with pytest.raises(RuntimeError, match="Invalid branch export checkpoint record"):
        checkpoint_from_record(tmp_path, payload)


def test_checkpoint_from_record_accepts_valid_required_fields(
    tmp_path: Path,
) -> None:
    payload = {
        "workspace_state_artifact": "implement/workspace-state.json",
        "node_id": "implement",
        "task_id": "alpha",
        "result_commit": "a" * 40,
        "result_tree": "b" * 40,
        "result_ref": "refs/crewplane/run/implement/result",
        "bundle": {
            "path": "implement/workspace-bundles/result.bundle",
            "sha256": "c" * 64,
            "size_bytes": 12,
        },
    }

    checkpoint = checkpoint_from_record(tmp_path, payload)

    assert checkpoint is not None
    assert checkpoint.state_path == tmp_path / "implement/workspace-state.json"
    assert checkpoint.node_id == "implement"
    assert checkpoint.bundle_size_bytes == 12


def test_fulfill_branch_exports_from_history_writes_duplicate_skip_record(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/history")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "feature result\n",
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    history = history_record_for_output(output)

    records = fulfill_branch_exports_from_history(plan, history)

    assert len(records) == 1
    assert records[0].parent == output.stages_dir / "workspace-exports"
    assert records[0].name == build_workspace_export_filename("primary")
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["run_id"] == history.manifest.run_id
    assert record["run_key_name"] == history.manifest.run_key_name
    assert record["branch_name"] == "feature/history"
    assert run_git_text(repo, "rev-parse", "refs/heads/feature/history") == (
        result_commit
    )


def test_fulfill_branch_exports_rejects_unresolved_workspace_mutator(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/unresolved-mutator")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "feature result\n",
    )
    state_path = write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["workspace_mutator"] = {
        "status": "unresolved",
        "operation": "success_finalizer",
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(RuntimeError, match="unresolved workspace mutator"):
        fulfill_branch_exports(plan, output)

    assert not run_git_text(
        repo,
        "branch",
        "--list",
        "feature/unresolved-mutator",
    )


@pytest.mark.parametrize(
    (
        "branch_state",
        "expected_operation",
        "expected_branch_exists_before",
        "expected_preview_operation",
        "expected_preview_branch_exists_before",
        "expected_preview_branch_exists_after",
        "prepared_origin",
    ),
    (
        ("absent", "created", False, "created", False, False, "current_run"),
        (
            "exact",
            "verified_existing",
            True,
            "verified_existing",
            True,
            True,
            "verified_history",
        ),
        (
            "disappears_after_probe",
            "created",
            False,
            "verified_existing",
            True,
            True,
            "verified_history",
        ),
    ),
)
def test_history_fulfillment_recovers_prepared_export_when_policy_is_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    branch_state: str,
    expected_operation: str,
    expected_branch_exists_before: bool,
    expected_preview_operation: str,
    expected_preview_branch_exists_before: bool,
    expected_preview_branch_exists_after: bool,
    prepared_origin: str,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    enabled_plan = branch_export_plan(
        repo,
        tmp_path,
        branch_name="feature/prepared",
    )
    disabled_plan = branch_export_plan(
        repo,
        tmp_path,
        branch_name="feature/prepared",
        create_branch=False,
    )
    assert disabled_plan.workflow_signature == enabled_plan.workflow_signature
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "feature result\n",
    )
    write_workspace_state(
        output.stages_dir,
        enabled_plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    history = history_record_for_output(output)
    preview = preview_branch_exports_from_history(enabled_plan, history)[0]
    checkpoint = checkpoint_from_record(output.stages_dir, preview)
    assert checkpoint is not None
    source = enabled_plan.workspace_source
    policy = enabled_plan.nodes[0].workspace_policy
    assert source is not None
    assert policy is not None
    prepared = prepared_branch_export_record(
        enabled_plan,
        history.manifest.run_id,
        history.manifest.run_key_name,
        source.repository_id,
        "primary",
        "feature/prepared",
        "refs/heads/feature/prepared",
        checkpoint,
        policy,
        prepared_origin,
        "initial",
    )
    prepared_record_path = output.write_workspace_export("primary", prepared)
    prepared_record_bytes = prepared_record_path.read_bytes()
    branch_ref = "refs/heads/feature/prepared"
    if branch_state != "absent":
        run_git_text(
            repo,
            "update-ref",
            branch_ref,
            result_commit,
        )

    recovery_preview = preview_branch_exports_from_history(disabled_plan, history)[0]

    assert recovery_preview["status"] == "fulfilled"
    assert recovery_preview["operation"] == expected_preview_operation
    assert recovery_preview["operation_origin"] == prepared_origin
    assert recovery_preview["recovery_mode"] == "prepared_record"
    assert (
        recovery_preview["branch_exists_before"]
        is expected_preview_branch_exists_before
    )
    assert (
        recovery_preview["branch_exists_after"] is expected_preview_branch_exists_after
    )
    assert recovery_preview["dry_run"] is True
    assert prepared_record_path.read_bytes() == prepared_record_bytes
    if branch_state == "absent":
        assert run_git_text(repo, "branch", "--list", "feature/prepared") == ""
    else:
        assert run_git_text(repo, "rev-parse", branch_ref) == result_commit
    if branch_state == "disappears_after_probe":
        original_create_branch_export_ref = (
            branch_export_fulfillment.create_branch_export_ref
        )

        def delete_branch_before_prepared_recovery(
            source: WorkspaceSourceSnapshot,
            requested_branch_ref: str,
            prepared_checkpoint: BranchExportCheckpoint,
            allow_existing: bool = False,
            allow_create: bool = True,
        ) -> BranchExportOperation:
            assert requested_branch_ref == branch_ref
            run_git_text(repo, "update-ref", "-d", requested_branch_ref)
            return original_create_branch_export_ref(
                source,
                requested_branch_ref,
                prepared_checkpoint,
                allow_existing=allow_existing,
                allow_create=allow_create,
            )

        monkeypatch.setattr(
            branch_export_attempts,
            "create_branch_export_ref",
            delete_branch_before_prepared_recovery,
        )

    records = fulfill_branch_exports_from_history(disabled_plan, history)

    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["status"] == "fulfilled"
    assert record["operation"] == expected_operation
    assert record["operation_origin"] == prepared_origin
    assert record["recovery_mode"] == "prepared_record"
    assert record["branch_exists_before"] is expected_branch_exists_before
    assert record["branch_exists_after"] is True
    assert run_git_text(repo, "rev-parse", "refs/heads/feature/prepared") == (
        result_commit
    )


@pytest.mark.parametrize("corrupt_checkpoint", (False, True))
def test_history_fulfillment_preserves_invalid_terminal_evidence_across_retries(
    tmp_path: Path,
    corrupt_checkpoint: bool,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/invalid-record")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "feature result\n",
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    history = history_record_for_output(output)
    source = plan.workspace_source
    assert source is not None
    invalid_record = skipped_branch_export_record(
        plan,
        history.manifest.run_id,
        history.manifest.run_key_name,
        "primary",
        "implement",
        operation_origin="verified_history",
        repository_id=source.repository_id,
    )
    invalid_record["workflow_signature"] = "contradictory-signature"
    record_path = output.write_workspace_export("primary", invalid_record)
    original_record = record_path.read_bytes()

    preview = preview_branch_exports_from_history(plan, history)[0]

    assert preview["status"] == "failed_verification"
    assert "terminal history contradicts" in str(preview["failure_message"])
    assert preview["dry_run"] is True
    assert record_path.read_bytes() == original_record
    if corrupt_checkpoint:
        bundle_path.write_bytes(b"corrupted branch export bundle")

    for attempt in range(1, 3):
        with pytest.raises(RuntimeError) as exc_info:
            fulfill_branch_exports_from_history(plan, history)

        if corrupt_checkpoint:
            assert "bundle" in str(exc_info.value)
        else:
            assert "terminal history contradicts" in str(exc_info.value)
        assert record_path.read_bytes() == original_record, (
            f"record changed on attempt {attempt}"
        )
        assert run_git_text(repo, "branch", "--list", "feature/invalid-record") == ""


def test_fulfill_branch_exports_verifies_exact_branch_for_resumed_node(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/resumed-exact")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "feature result\n",
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    run_git_text(
        repo,
        "update-ref",
        "refs/heads/feature/resumed-exact",
        result_commit,
    )

    records = fulfill_branch_exports(
        plan,
        output,
        resumed_node_ids=("implement",),
    )

    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["status"] == "fulfilled"
    assert record["operation"] == "verified_existing"
    assert record["operation_origin"] == "verified_history"
    assert record["recovery_mode"] == "initial"
    assert record["branch_exists_before"] is True
    assert record["branch_exists_after"] is True


def test_history_fulfillment_audits_exact_branch_race_from_locked_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/raced-exact")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "feature result\n",
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    history = history_record_for_output(output)
    original_branch_ref_exists = branch_export_git.branch_ref_exists
    raced = False

    def create_exact_branch_after_probe(
        source: WorkspaceSourceSnapshot,
        branch_ref: str,
    ) -> bool:
        nonlocal raced
        exists = original_branch_ref_exists(source, branch_ref)
        if not raced:
            assert exists is False
            raced = True
            run_git_text(repo, "update-ref", branch_ref, result_commit)
        return exists

    monkeypatch.setattr(
        branch_export_attempts,
        "branch_ref_exists",
        create_exact_branch_after_probe,
    )

    records = fulfill_branch_exports_from_history(plan, history)

    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["status"] == "fulfilled"
    assert record["operation"] == "verified_existing"
    assert record["branch_exists_before"] is True
    assert record["branch_exists_after"] is True
    replayed_records = fulfill_branch_exports_from_history(plan, history)
    replayed = json.loads(replayed_records[0].read_text(encoding="utf-8"))
    assert replayed == record


def test_history_fulfillment_does_not_create_branch_that_disappears_after_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/disappeared")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "feature result\n",
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    history = history_record_for_output(output)
    branch_ref = "refs/heads/feature/disappeared"
    run_git_text(repo, "update-ref", branch_ref, result_commit)
    original_create_branch_export_ref = (
        branch_export_fulfillment.create_branch_export_ref
    )

    def delete_branch_before_locked_decision(
        source: WorkspaceSourceSnapshot,
        requested_branch_ref: str,
        checkpoint: BranchExportCheckpoint,
        allow_existing: bool = False,
        allow_create: bool = True,
    ) -> BranchExportOperation:
        assert requested_branch_ref == branch_ref
        run_git_text(repo, "update-ref", "-d", requested_branch_ref)
        return original_create_branch_export_ref(
            source,
            requested_branch_ref,
            checkpoint,
            allow_existing=allow_existing,
            allow_create=allow_create,
        )

    monkeypatch.setattr(
        branch_export_attempts,
        "create_branch_export_ref",
        delete_branch_before_locked_decision,
    )

    with pytest.raises(RuntimeError, match="disappeared before locked verification"):
        fulfill_branch_exports_from_history(plan, history)

    record_path = (
        output.stages_dir
        / "workspace-exports"
        / build_workspace_export_filename("primary")
    )
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["status"] == "failed_verification"
    assert run_git_text(repo, "branch", "--list", "feature/disappeared") == ""


def test_fulfill_branch_exports_ignores_inherited_git_transport_restrictions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/restricted-env")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "feature result\n",
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    history = history_record_for_output(output)
    monkeypatch.setenv("GIT_PROTOCOL_FROM_USER", "0")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "https")

    fulfill_branch_exports_from_history(plan, history)

    assert (
        run_git_text(
            repo,
            "rev-parse",
            "refs/heads/feature/restricted-env",
        )
        == result_commit
    )


def test_fulfill_branch_exports_writes_skipped_record_when_disabled(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(
        repo,
        tmp_path,
        branch_name=None,
        create_branch=False,
    )
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "feature result\n",
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    node_state_path = write_node_manifest(output, plan)

    records = fulfill_branch_exports(plan, output)

    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["status"] == "skipped"
    assert record["operation"] == "skipped"
    assert record["skip_reason"] == "create_branch_false"
    assert record["branch_name"] is None
    state = json.loads(
        (
            output.create_node_dir(node_artifact_request("implement"))
            / "workspace-state.json"
        ).read_text(encoding="utf-8")
    )
    assert state["branch_export"]["status"] == "skipped"
    assert state["branch_export"]["operation"] == "skipped"
    assert state["branch_export"]["record_artifact"].startswith("workspace-exports/")
    node_state = json.loads(node_state_path.read_text(encoding="utf-8"))
    manifest_state = node_state["workspace"]["states"][0]
    assert manifest_state["branch_export"]["status"] == "skipped"


def test_disabled_branch_export_preserves_broken_record_symlink(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(
        repo,
        tmp_path,
        branch_name="feature/disabled",
        create_branch=False,
    )
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "feature result\n",
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    record_path = (
        output.stages_dir
        / "workspace-exports"
        / build_workspace_export_filename("primary")
    )
    record_path.parent.mkdir(parents=True)
    missing_target = record_path.with_name("missing-record.json")
    record_path.symlink_to(missing_target)

    with pytest.raises(RuntimeError, match="missing or unsafe"):
        fulfill_branch_exports(plan, output)

    assert record_path.is_symlink()
    assert record_path.readlink() == missing_target


def test_preview_branch_exports_verifies_without_creating_ref(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/preview")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "feature result\n",
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    history = history_record_for_output(output)

    records = preview_branch_exports_from_history(plan, history)

    assert len(records) == 1
    record = records[0]
    assert record["dry_run"] is True
    assert record["status"] == "fulfilled"
    assert record["operation"] == "created"
    assert record["branch_exists_before"] is False
    assert record["branch_exists_after"] is False
    assert not run_git_text(repo, "branch", "--list", "feature/preview")


def test_preview_and_history_fulfillment_verify_existing_expected_branch(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/preview")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    result_commit, result_tree, result_ref, bundle_path = write_result_bundle(
        repo,
        output.create_node_dir(node_artifact_request("implement")),
        "feature result\n",
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        result_commit,
        result_tree,
        result_ref,
        bundle_path,
    )
    run_git_text(repo, "update-ref", "refs/heads/feature/preview", result_commit)
    history = history_record_for_output(output)

    preview_records = preview_branch_exports_from_history(plan, history)
    record_paths = fulfill_branch_exports_from_history(plan, history)

    assert len(preview_records) == 1
    assert preview_records[0]["dry_run"] is True
    assert preview_records[0]["status"] == "fulfilled"
    assert preview_records[0]["operation"] == "verified_existing"
    assert len(record_paths) == 1
    record = json.loads(record_paths[0].read_text(encoding="utf-8"))
    assert record["status"] == "fulfilled"
    assert record["operation"] == "verified_existing"
    assert record["branch_exists_before"] is True
    assert record["branch_exists_after"] is True


def test_preview_branch_exports_rejects_prerequisite_bundle_chain(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/preview")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    first, second = create_prerequisite_bundle_chain(
        repo,
        output.stages_dir / "prepare" / "workspace-bundles" / "first.bundle",
        output.create_node_dir(node_artifact_request("implement"))
        / "workspace-bundles"
        / "second.bundle",
    )
    if git_commit_exists(repo, first.commit) or git_commit_exists(repo, second.commit):
        pytest.skip("git retained the test commits after pruning")
    source_refs_before = run_git_text(
        repo,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
    )
    write_workspace_state(
        output.stages_dir,
        plan,
        second.commit,
        second.tree,
        second.ref,
        second.path,
        source_ref=WorktreeSourceRef(
            source_kind="node",
            source_node_id="prepare",
            source_commit=first.commit,
            source_tree=first.tree,
            candidate_sequence=1,
            bundle_path=first.path,
            bundle_sha256=first.sha256,
            bundle_size_bytes=first.size_bytes,
            bundle_ref=first.ref,
        ),
    )
    history = history_record_for_output(output)

    records = preview_branch_exports_from_history(plan, history)

    assert len(records) == 1
    record = records[0]
    assert record["dry_run"] is True
    assert record["status"] == "failed_verification"
    assert "advertises prerequisites" in record["failure_message"]
    assert not git_commit_exists(repo, first.commit)
    assert not git_commit_exists(repo, second.commit)
    assert not run_git_text(repo, "branch", "--list", "feature/preview")
    assert (
        run_git_text(repo, "for-each-ref", "--format=%(refname) %(objectname)")
        == source_refs_before
    )


def test_preview_and_fulfillment_reject_ambient_omitted_bundle_prerequisite(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/omitted-upstream")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    first, second = create_prerequisite_bundle_chain(
        repo,
        output.stages_dir / "prepare" / "workspace-bundles" / "first.bundle",
        output.create_node_dir(node_artifact_request("implement"))
        / "workspace-bundles"
        / "second.bundle",
    )
    run_git_text(
        repo,
        "fetch",
        first.path.as_posix(),
        f"{first.ref}:refs/crewplane/test/ambient-first",
    )
    run_git_text(
        repo,
        "fetch",
        second.path.as_posix(),
        f"{second.ref}:refs/crewplane/test/ambient-second",
    )
    run_git_text(repo, "update-ref", "-d", "refs/crewplane/test/ambient-first")
    run_git_text(repo, "update-ref", "-d", "refs/crewplane/test/ambient-second")
    assert git_commit_exists(repo, first.commit)
    assert git_commit_exists(repo, second.commit)
    write_workspace_state(
        output.stages_dir,
        plan,
        second.commit,
        second.tree,
        second.ref,
        second.path,
    )
    history = history_record_for_output(output)

    preview_records = preview_branch_exports_from_history(plan, history)
    with pytest.raises(RuntimeError) as exc_info:
        fulfill_branch_exports_from_history(plan, history)

    assert len(preview_records) == 1
    assert preview_records[0]["status"] == "failed_verification"
    failure_message = str(preview_records[0]["failure_message"])
    assert failure_message.startswith(
        "Workspace lineage source verification failed while validating recorded "
        "Git artifacts:"
    )
    assert str(exc_info.value) == failure_message
    assert "Git did not provide diagnostic output" not in failure_message
    assert "crewplane-lineage-verify-" not in failure_message
    assert "Command '['git'" not in failure_message
    assert not run_git_text(
        repo,
        "branch",
        "--list",
        "feature/omitted-upstream",
    )


def test_preview_branch_exports_rejects_sha256_prerequisite_bundles(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    try:
        repo = create_git_repo(tmp_path, object_format="sha256")
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"git sha256 object format is unavailable: {exc}")
    plan = branch_export_plan(repo, tmp_path, branch_name="feature/preview")
    output = OutputManager("workspace", base_dir=tmp_path / "artifacts")
    first, second = create_prerequisite_bundle_chain(
        repo,
        output.stages_dir / "prepare" / "workspace-bundles" / "first.bundle",
        output.create_node_dir(node_artifact_request("implement"))
        / "workspace-bundles"
        / "second.bundle",
    )
    if git_commit_exists(repo, first.commit) or git_commit_exists(repo, second.commit):
        pytest.skip("git retained the test commits after pruning")
    write_workspace_state(
        output.stages_dir,
        plan,
        second.commit,
        second.tree,
        second.ref,
        second.path,
        source_ref=WorktreeSourceRef(
            source_kind="node",
            source_node_id="prepare",
            source_commit=first.commit,
            source_tree=first.tree,
            candidate_sequence=1,
            bundle_path=first.path,
            bundle_sha256=first.sha256,
            bundle_size_bytes=first.size_bytes,
            bundle_ref=first.ref,
        ),
    )
    history = history_record_for_output(output)

    records = preview_branch_exports_from_history(plan, history)

    assert len(records) == 1
    record = records[0]
    assert record["dry_run"] is True
    assert record["status"] == "failed_verification"
    assert "advertises prerequisites" in record["failure_message"]
    assert not git_commit_exists(repo, first.commit)
    assert not git_commit_exists(repo, second.commit)
    assert not run_git_text(repo, "branch", "--list", "feature/preview")
