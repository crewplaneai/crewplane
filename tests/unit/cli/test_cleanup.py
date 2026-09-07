from __future__ import annotations

import json
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Never
from unittest.mock import Mock

import pytest
from rich.console import Console
from typer.testing import CliRunner

import crewplane.cli.cleanup as cleanup_cli
import crewplane.cli.workspace_cleanup.eligibility as cleanup_eligibility
import crewplane.cli.workspace_cleanup.execution as cleanup_execution
import crewplane.cli.workspace_cleanup.run_artifacts as cleanup_run_artifacts
import crewplane.runtime.workspace.cleanup as workspace_cleanup
from crewplane.artifacts.resume.validation import validate_resume_frontier
from crewplane.cli.app import app
from crewplane.cli.workspace_cleanup.context import cleanup_repository_id
from crewplane.cli.workspace_cleanup_evidence import WorkspaceCleanupEvidence
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.runtime.workspace.state import WorkspaceStateRetention
from crewplane.version import SCHEMA_VERSION
from tests.helpers.resume import (
    attach_workspace_descriptor,
    make_node_state,
    make_plan,
    make_run_manifest,
    write_node_state,
    write_result,
    write_run_manifest,
)
from tests.helpers.resume_validation import (
    attach_git_workspace_source,
    snapshot_workspace_state_payload,
    source_record,
)
from tests.helpers.workspace_records import (
    WORKTREE_CONTRACT_PAYLOAD,
    workspace_selection_record,
)


def test_cleanup_workspaces_defaults_to_advisory_dry_run(tmp_path: Path) -> None:
    project_root, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
    )

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix()],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert "Would remove 1 workspace path(s)" in result.output
    assert workspace_path.exists()
    assert project_root.exists()


def test_cleanup_workspaces_yes_removes_paths(tmp_path: Path) -> None:
    _, config_path, workspace_path = _cleanup_project(tmp_path, initialize_git=True)

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Removed 1 workspace path(s)" in result.output
    assert not workspace_path.exists(), result.output


def test_cleanup_workspaces_rechecks_exact_worktree_identity_before_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    state_path = (
        project_root / ".crewplane/execution-stages/run-1/node/workspace-state.json"
    )
    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
    expected_git_dir = Path(state_payload["execution"]["worktree_git_dir"])
    original_remove = workspace_cleanup.remove_unknown_workspace_path
    replacement_marker = workspace_path / "checkout" / "replacement.txt"

    def replace_checkout_before_removal(
        path: Path,
        expected_common_git_dir: Path | None,
        expected_worktree_git_dir: Path | None,
    ) -> None:
        checkout_root = path / "checkout"
        _git(
            project_root,
            "worktree",
            "move",
            checkout_root.as_posix(),
            (tmp_path / "moved-original-checkout").as_posix(),
        )
        _git(
            project_root,
            "worktree",
            "add",
            "--detach",
            checkout_root.as_posix(),
            "HEAD",
        )
        replacement_marker.write_text("replacement", encoding="utf-8")
        replacement_git_dir = Path(
            _git(checkout_root, "rev-parse", "--git-dir").strip()
        ).resolve()
        assert replacement_git_dir != expected_git_dir
        original_remove(
            path,
            expected_common_git_dir,
            expected_worktree_git_dir,
        )

    monkeypatch.setattr(
        workspace_cleanup,
        "remove_unknown_workspace_path",
        replace_checkout_before_removal,
    )

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
    )

    assert result.exit_code == 1
    assert "does not match persisted" in result.output
    assert "identity" in result.output
    assert replacement_marker.read_text(encoding="utf-8") == "replacement"
    retained = json.loads(state_path.read_text(encoding="utf-8"))
    assert retained["workspace"]["retention"] == "retained"


def test_cleanup_workspaces_retains_claim_without_planned_workspace_policy(
    tmp_path: Path,
) -> None:
    project_root, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    plan_path = (
        project_root / ".crewplane/execution-stages/run-1/preflight/execution-plan.json"
    )
    plan_payload = json.loads(plan_path.read_text(encoding="utf-8"))
    plan_payload["nodes"][0]["workspace_policy"] = None
    plan_path.write_text(json.dumps(plan_payload), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert "Removed 0 workspace path(s)" in result.output
    assert "workspace evidence for the run is malformed" in result.output
    assert workspace_path.exists()


def test_cleanup_workspaces_retains_claim_that_conflicts_with_plan_identity(
    tmp_path: Path,
) -> None:
    project_root, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    state_path = (
        project_root / ".crewplane/execution-stages/run-1/node/workspace-state.json"
    )
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["workflow_signature"] = "different-workflow-signature"
    publication = payload["ref_publication"]
    publication["phase"] = "published"
    destinations = publication["destinations"]
    ref_targets = {
        destination["name"]: destination["target_oid"]
        for destination in destinations.values()
    }
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    for ref_name, target_oid in ref_targets.items():
        _git(project_root, "update-ref", ref_name, target_oid)

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert "Removed 0 workspace path(s)" in result.output
    assert "workspace evidence for the run is malformed" in result.output
    assert workspace_path.exists()
    for ref_name, target_oid in ref_targets.items():
        assert _git(project_root, "rev-parse", ref_name).strip() == target_oid


def test_cleanup_workspaces_retains_reappeared_deleted_workspace(
    tmp_path: Path,
) -> None:
    project_root, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    state_path = (
        project_root / ".crewplane/execution-stages/run-1/node/workspace-state.json"
    )
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["workspace"]["retention"] = "deleted"
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    replacement_path = workspace_path / "checkout" / "replacement.txt"
    replacement_path.write_text("replacement", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert "Removed 0 workspace path(s)" in result.output
    assert "reappeared after deletion" in result.output
    assert replacement_path.read_text(encoding="utf-8") == "replacement"


@pytest.mark.parametrize("moved_temporary_ref", [False, True])
def test_cleanup_workspaces_preserves_resume_frontier(
    tmp_path: Path,
    moved_temporary_ref: bool,
) -> None:
    plan, project_root = attach_git_workspace_source(tmp_path, make_plan())
    repository_id_value = cleanup_repository_id(project_root, all_projects=False)
    assert repository_id_value is not None
    assert plan.workspace_source is not None
    workspace_policy = workspace_selection_record(enabled=True, kind="snapshot")
    plan = plan.model_copy(
        update={
            "workspace_source": plan.workspace_source.model_copy(
                update={"repository_id": repository_id_value}
            ),
            "nodes": [
                plan.nodes[0].model_copy(update={"workspace_policy": workspace_policy}),
                plan.nodes[1],
            ],
        }
    )
    state_dir = project_root / ".crewplane"
    source = source_record(state_dir, status="failed")
    plan = plan.model_copy(
        update={
            "run_id": source.manifest.run_id,
            "run_key_name": source.manifest.run_key_name,
            "project_root": project_root.as_posix(),
            "context_root": source.run_dir.as_posix(),
            "manifest_root": (source.run_dir / "manifests").as_posix(),
        }
    )
    plan_path = source.run_dir / source.manifest.preflight_plan_path
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    result_descriptor = write_result(
        source.results_dir,
        "a-result.md",
        "a output",
    )
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", [result_descriptor]),
    )
    cache_root = tmp_path / "cache"
    workspace_path = (
        cache_root
        / "snapshots"
        / repository_id_value
        / source.manifest.run_key_name
        / "a-alpha-round1"
    )
    checkout_root = workspace_path / "checkout"
    checkout_root.mkdir(parents=True)
    state_payload = snapshot_workspace_state_payload(source, plan, "alpha")
    workspace_payload = state_payload["workspace"]
    assert isinstance(workspace_payload, dict)
    workspace_payload["cache_key"] = workspace_path.name
    state_payload["execution"] = {
        "cache_root": cache_root.as_posix(),
        "workspace_path": workspace_path.as_posix(),
        "checkout_root": checkout_root.as_posix(),
        "effective_cwd": checkout_root.as_posix(),
        "worktree_git_dir": None,
    }
    if moved_temporary_ref:
        assert plan.workspace_source is not None
        temporary_ref = (
            "refs/crewplane/runs/workflow--source/imports/a/a-alpha-round1/moved"
        )
        state_payload["temporary_refs"] = [
            {
                "phase": "prepared",
                "name": temporary_ref,
                "target_oid": plan.workspace_source.run_base_commit,
                "owner_run_id": source.manifest.run_id,
                "owner_node_id": "a",
                "owner_task_id": "alpha",
                "owner_role": "executor",
                "owner_round_num": 1,
                "owner_audit_round_num": None,
                "repository_id": repository_id_value,
            }
        ]
        (project_root / "moved.txt").write_text("moved\n", encoding="utf-8")
        _git(project_root, "add", "moved.txt")
        _git(project_root, "commit", "-m", "move temporary ref")
        _git(project_root, "update-ref", temporary_ref, "HEAD")
    state_path = source.run_dir / "a" / "workspace-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state_payload), encoding="utf-8")
    attach_workspace_descriptor(source.run_dir, plan, "a")
    config_path = state_dir / "config.yml"
    config_path.write_text(
        "\n".join(
            [
                f'version: "{SCHEMA_VERSION}"',
                "agents:",
                "  alpha:",
                '    cli_cmd: ["mock"]',
                '    default_model: "test"',
                "settings:",
                "  workspace:",
                "    enabled: true",
                f'    cache_root: "{cache_root.as_posix()}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    assert validate_resume_frontier(source, plan).resumed_node_ids == ("a",)

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    if moved_temporary_ref:
        assert result.exit_code == 1
        assert "Workspace temporary import ref moved and was retained" in result.output
    else:
        assert result.exit_code == 0, result.output
    assert not workspace_path.exists(), result.output
    assert (
        json.loads(state_path.read_text(encoding="utf-8"))["workspace"]["retention"]
        == "deleted"
    )
    assert validate_resume_frontier(source, plan).resumed_node_ids == ("a",)


@pytest.mark.parametrize(
    "failure",
    [
        PermissionError("read-only cache"),
        OSError("workspace I/O failure"),
    ],
)
def test_cleanup_workspaces_reports_mutation_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: OSError,
) -> None:
    _, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
    )

    def fail_cleanup(
        context: object,
        destructive: bool,
    ) -> Never:
        del context, destructive
        raise failure

    monkeypatch.setattr(cleanup_cli, "execute_workspace_cleanup", fail_cleanup)

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
    )

    assert result.exit_code == 1
    assert f"Cleanup failed: {failure}" in result.output
    assert not isinstance(result.exception, OSError)
    assert workspace_path.exists()


def test_execute_workspace_cleanup_preview_never_refreshes_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, config_path, _ = _cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    _write_cleanup_node_manifest(project_root, "run-1")
    refresh = Mock(side_effect=AssertionError("preview cleanup refreshed descriptors"))
    monkeypatch.setattr(
        cleanup_run_artifacts,
        "refresh_node_workspace_descriptor",
        refresh,
    )

    context = _resolve_workspace_cleanup_context(config_path)
    result = cleanup_cli.execute_workspace_cleanup(context, destructive=False)

    assert result.selected_count == 1
    refresh.assert_not_called()


def test_execute_workspace_cleanup_refreshes_sorted_union_after_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    _write_cleanup_node_manifest(project_root, "run-1")
    _copy_cleanup_run(project_root, "a-run")
    _copy_cleanup_run(project_root, "z-run")
    cache_root = workspace_path.parents[3]
    expected = _removed_cleanup_result(cache_root, "z-run", "a-run")
    monkeypatch.setattr(
        cleanup_execution,
        "cleanup_workspace_cache",
        Mock(return_value=expected),
    )
    refresh_calls: list[str] = []

    def record_refresh(
        node: object,
        plan: PreflightExecutionPlan,
        store: object,
    ) -> None:
        del node, store
        refresh_calls.append(plan.run_key_name)

    monkeypatch.setattr(
        cleanup_run_artifacts,
        "refresh_node_workspace_descriptor",
        record_refresh,
    )

    context = _resolve_workspace_cleanup_context(config_path)
    assert cleanup_cli.execute_workspace_cleanup(context, destructive=True) is expected
    assert refresh_calls == ["a-run", "run-1", "z-run"]


def test_execute_workspace_cleanup_preserves_failure_and_notes_refresh_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, config_path, _ = _cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    _write_cleanup_node_manifest(project_root, "run-1")
    cleanup_error = ValueError("cleanup failed")
    cleanup_error.add_note("existing cleanup note")
    monkeypatch.setattr(
        cleanup_execution,
        "cleanup_workspace_cache",
        Mock(side_effect=cleanup_error),
    )
    refresh_calls: list[str] = []

    def fail_refresh(
        node: object,
        plan: PreflightExecutionPlan,
        store: object,
    ) -> Never:
        del node, store
        refresh_calls.append(plan.run_key_name)
        raise RuntimeError("refresh failed")

    monkeypatch.setattr(
        cleanup_run_artifacts,
        "refresh_node_workspace_descriptor",
        fail_refresh,
    )

    context = _resolve_workspace_cleanup_context(config_path)
    with pytest.raises(ValueError) as exc_info:
        cleanup_cli.execute_workspace_cleanup(context, destructive=True)

    assert exc_info.value is cleanup_error
    assert exc_info.value.__notes__ == [
        "existing cleanup note",
        "Workspace descriptor refresh after partial cleanup failed: refresh failed",
    ]
    assert refresh_calls == ["run-1"]


@pytest.mark.parametrize(
    ("failure_stage", "expected_message", "expected_cause_type"),
    [
        (
            "manifest",
            "Cannot refresh workspace descriptors for 'run-1'.",
            None,
        ),
        ("plan_location", "Preflight plan is missing for run 'run-1'.", None),
        ("plan_content", "Preflight plan is invalid for run 'run-1'.", ValueError),
        ("identity", "Preflight plan identity is invalid for run 'run-1'.", None),
    ],
)
def test_load_cleanup_preflight_plan_preserves_first_failure_and_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
    expected_message: str,
    expected_cause_type: type[BaseException] | None,
) -> None:
    project_root, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    _write_cleanup_node_manifest(project_root, "run-1")
    run_dir = project_root / ".crewplane/execution-stages/run-1"
    manifest_path = run_dir / "manifests/run.json"
    plan_path = run_dir / "preflight/execution-plan.json"
    if failure_stage == "manifest":
        manifest_path.write_text("{}", encoding="utf-8")
        plan_path.unlink()
    elif failure_stage == "plan_location":
        plan_path.unlink()
    elif failure_stage == "plan_content":
        plan_path.write_text("{}", encoding="utf-8")
    else:
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
        payload["run_id"] = "other-run"
        plan_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        cleanup_execution,
        "cleanup_workspace_cache",
        Mock(
            return_value=_removed_cleanup_result(
                workspace_path.parents[3],
                "run-1",
            )
        ),
    )

    context = _resolve_workspace_cleanup_context(config_path)
    with pytest.raises(RuntimeError) as exc_info:
        cleanup_cli.execute_workspace_cleanup(context, destructive=True)

    assert str(exc_info.value) == expected_message
    if expected_cause_type is None:
        assert exc_info.value.__cause__ is None
    else:
        assert isinstance(exc_info.value.__cause__, expected_cause_type)


@pytest.mark.parametrize(
    ("manifest_kind", "expected_reason"),
    [
        ("invalid", "run manifest is invalid"),
        ("mismatched", "run manifest is active or mismatched"),
        ("running", "run manifest is active or mismatched"),
    ],
)
def test_workspace_manifest_rejection_precedes_activity_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest_kind: str,
    expected_reason: str,
) -> None:
    project_root, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
        create_workspace=False,
    )
    workspace_path.mkdir(parents=True)
    (workspace_path / "orphan.txt").write_text("orphan", encoding="utf-8")
    state_dir = project_root / ".crewplane"
    manifest_path = state_dir / "execution-stages/run-1/manifests/run.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        _workspace_manifest_content(manifest_kind),
        encoding="utf-8",
    )
    evidence = Mock(spec=WorkspaceCleanupEvidence)
    evidence.ref_cleanup_run_keys.return_value = ("run-1",)
    evidence.absent_state_projections.return_value = ()
    evidence.status_for_cache_key.return_value = None
    evidence.decision.return_value = None
    monkeypatch.setattr(
        cleanup_execution,
        "WorkspaceCleanupEvidence",
        Mock(return_value=evidence),
    )
    activity_failure = AssertionError("manifest rejection must precede activity checks")
    run_lock = Mock(side_effect=activity_failure)
    provider_processes = Mock(side_effect=activity_failure)
    monkeypatch.setattr(cleanup_eligibility, "run_lock_activity", run_lock)
    monkeypatch.setattr(
        cleanup_eligibility,
        "ensure_no_live_provider_processes",
        provider_processes,
    )

    context = _resolve_workspace_cleanup_context(config_path, orphans=True)
    result = cleanup_cli.execute_workspace_cleanup(context, destructive=False)

    assert len(result.entries) == 1
    assert result.entries[0].retained_reason == expected_reason
    run_lock.assert_not_called()
    provider_processes.assert_not_called()


def test_cleanup_workspaces_yes_retains_active_run_assets(tmp_path: Path) -> None:
    project_root, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
        run_status="running",
    )
    _git(project_root, "update-ref", "refs/crewplane/runs/run-1/node/a", "HEAD")

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Removed 0 workspace path(s)" in result.output
    assert "retained: workspace state is running" in result.output
    assert workspace_path.exists()
    assert _git(project_root, "for-each-ref", "refs/crewplane/runs/run-1") != ""


def test_cleanup_workspaces_yes_allows_distinct_terminal_node_and_run_states(
    tmp_path: Path,
) -> None:
    project_root, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    write_run_manifest(
        project_root / ".crewplane",
        make_run_manifest(
            run_id="run-1",
            run_key_name="run-1",
            status="failed",
        ),
    )
    _git(project_root, "update-ref", "refs/crewplane/runs/run-1/node/a", "HEAD")

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Removed 1 workspace path(s)" in result.output
    assert not workspace_path.exists()
    assert _git(project_root, "for-each-ref", "refs/crewplane/runs/run-1") != ""


def test_cleanup_workspaces_yes_preserves_unrecorded_run_refs(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    project_root, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    _git(project_root, "update-ref", "refs/crewplane/runs/run-1/node/a", "HEAD")

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Removed 1 workspace path(s)" in result.output
    assert not workspace_path.exists()
    assert _git(project_root, "for-each-ref", "refs/crewplane/runs/run-1") != ""


def test_cleanup_workspaces_reconciles_hydrated_run_temporary_ref_evidence(
    tmp_path: Path,
) -> None:
    project_root, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    state_path = (
        project_root / ".crewplane/execution-stages/run-1/node/workspace-state.json"
    )
    nested_state_path = (
        state_path.parents[1] / "custom/build-stage/workspace-state.json"
    )
    nested_state_path.parent.mkdir(parents=True)
    state_path.replace(nested_state_path)
    state_path = nested_state_path
    plan_path = state_path.parents[2] / "preflight/execution-plan.json"
    plan_payload = json.loads(plan_path.read_text(encoding="utf-8"))
    nested_contract = plan_payload["nodes"][0]["artifact_contract"]
    nested_contract["stage_path"] = "custom/build-stage"
    nested_contract["output_path"] = "custom/build-stage/output.md"
    nested_contract["log_path"] = "custom/build-stage/logs"
    nested_contract["result_path"] = "custom/build-stage/output.md"
    plan_path.write_text(json.dumps(plan_payload), encoding="utf-8")
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    workspace = payload["workspace"]
    execution = payload["execution"]
    payload["resume_origin"] = {
        "source_run_id": "source-run",
        "source_run_key_name": "source-run-key",
        "source_node_id": "node",
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
    payload.pop("ref_publication")
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    _git(
        project_root,
        "worktree",
        "remove",
        "--force",
        (workspace_path / "checkout").as_posix(),
    )
    workspace_path.rmdir()

    target_oid = _git(project_root, "rev-parse", "HEAD^{commit}").strip()
    temporary_ref = (
        "refs/crewplane/runs/run-1/imports/node/"
        "node-branch-export-primary-round0/temporary"
    )
    _git(project_root, "update-ref", temporary_ref, target_oid)
    evidence_path = state_path.with_name("workspace-temporary-refs-interrupted.json")
    evidence_path.write_text(
        json.dumps(
            {
                "evidence_kind": "temporary_ref_cleanup",
                "run_id": "run-1",
                "run_key_name": "run-1",
                "node_id": "node",
                "task_id": "branch-export-primary",
                "role": "artifact_consumer",
                "round_num": 0,
                "audit_round_num": None,
                "git": {"repo_id": payload["git"]["repo_id"]},
                "temporary_refs": [
                    {
                        "phase": "prepared",
                        "name": temporary_ref,
                        "target_oid": target_oid,
                        "owner_run_id": "run-1",
                        "owner_node_id": "node",
                        "owner_task_id": "branch-export-primary",
                        "owner_role": "artifact_consumer",
                        "owner_round_num": 0,
                        "owner_audit_round_num": None,
                        "repository_id": payload["git"]["repo_id"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert "Removed 1 run-owned Git ref(s)." in result.output
    assert _git(project_root, "for-each-ref", temporary_ref).strip() == ""
    assert not evidence_path.exists()


@pytest.mark.parametrize(
    "filter_args",
    [
        ["--failed"],
        ["--older-than", "1d"],
        ["--orphans"],
    ],
)
def test_cleanup_workspaces_filters_preserve_refs_for_unselected_state(
    tmp_path: Path,
    filter_args: list[str],
) -> None:
    project_root, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    state_path = (
        project_root / ".crewplane/execution-stages/run-1/node/workspace-state.json"
    )
    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
    publication = state_payload["ref_publication"]
    publication["phase"] = "published"
    destinations = publication["destinations"]
    ref_targets = {
        destination["name"]: destination["target_oid"]
        for destination in destinations.values()
    }
    state_path.write_text(json.dumps(state_payload), encoding="utf-8")
    node_manifest_path = state_path.parents[1] / "manifests/nodes/node.json"
    node_manifest_path.parent.mkdir(parents=True)
    node_manifest_path.write_text("{}", encoding="utf-8")
    for ref_name, target_oid in ref_targets.items():
        _git(project_root, "update-ref", ref_name, target_oid)

    result = CliRunner().invoke(
        app,
        [
            "cleanup",
            "workspaces",
            "--config",
            config_path.as_posix(),
            *filter_args,
            "--yes",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert "Removed 0 workspace path(s)" in result.output
    assert workspace_path.exists()
    for ref_name, target_oid in ref_targets.items():
        assert _git(project_root, "rev-parse", ref_name).strip() == target_oid
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["ref_publication"]["phase"] == "published"


def test_cleanup_workspaces_orphans_removes_only_terminal_run_orphan(
    tmp_path: Path,
) -> None:
    project_root, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    orphan_path = workspace_path.parent / "orphan-round1"
    orphan_path.mkdir()
    (orphan_path / "file.txt").write_text("orphan", encoding="utf-8")
    _git(project_root, "update-ref", "refs/crewplane/runs/run-1/node/a", "HEAD")

    result = CliRunner().invoke(
        app,
        [
            "cleanup",
            "workspaces",
            "--config",
            config_path.as_posix(),
            "--orphans",
            "--yes",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Removed 1 workspace path(s)" in result.output
    assert workspace_path.exists()
    assert not orphan_path.exists()
    assert _git(project_root, "for-each-ref", "refs/crewplane/runs/run-1") != ""


def test_cleanup_workspaces_orphans_retains_unverifiable_run(tmp_path: Path) -> None:
    _, config_path, orphan_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
        create_workspace=False,
    )
    orphan_path.mkdir(parents=True)
    (orphan_path / "file.txt").write_text("orphan", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "cleanup",
            "workspaces",
            "--config",
            config_path.as_posix(),
            "--orphans",
            "--yes",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Removed 0 workspace path(s)" in result.output
    assert "run manifest is missing or unsafe" in result.output
    assert orphan_path.exists()


def test_cleanup_workspaces_removes_one_coherent_multi_generation_worktree(
    tmp_path: Path,
) -> None:
    project_root, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    state_path = (
        project_root / ".crewplane/execution-stages/run-1/node/workspace-state.json"
    )
    generation_one = json.loads(state_path.read_text(encoding="utf-8"))
    generation_two = deepcopy(generation_one)
    generation_two["workspace"]["reuse_generation"] = 2
    generation_two_path = state_path.with_name(
        "workspace-reuse-claim-node-generation-2.json"
    )
    generation_two_path.write_text(json.dumps(generation_two), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Removed 1 workspace path(s)" in result.output
    assert not workspace_path.exists()
    assert (
        json.loads(state_path.read_text(encoding="utf-8"))["workspace"]["retention"]
        == "deleted"
    )
    assert (
        json.loads(generation_two_path.read_text(encoding="utf-8"))["workspace"][
            "retention"
        ]
        == "deleted"
    )


def test_cleanup_workspaces_reconciles_generation_states_after_partial_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    state_path = (
        project_root / ".crewplane/execution-stages/run-1/node/workspace-state.json"
    )
    generation_one = json.loads(state_path.read_text(encoding="utf-8"))
    generation_one["status"] = "failed"
    generation_one["workspace"]["retention"] = "pending_cleanup"
    state_path.write_text(json.dumps(generation_one), encoding="utf-8")
    generation_two = deepcopy(generation_one)
    generation_two["status"] = "cancelled"
    generation_two["workspace"]["reuse_generation"] = 2
    generation_two_path = state_path.with_name(
        "workspace-reuse-claim-node-generation-2.json"
    )
    generation_two_path.write_text(json.dumps(generation_two), encoding="utf-8")
    original_update = workspace_cleanup.update_workspace_retention
    update_count = 0

    def fail_second_state_update(
        state_path_arg: Path,
        retention: WorkspaceStateRetention,
    ) -> None:
        nonlocal update_count
        update_count += 1
        if update_count == 2:
            raise OSError("injected second-state write failure")
        original_update(state_path_arg, retention)

    monkeypatch.setattr(
        workspace_cleanup,
        "update_workspace_retention",
        fail_second_state_update,
    )

    first_result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
    )

    assert first_result.exit_code == 1
    assert not workspace_path.exists()
    first_payload = json.loads(state_path.read_text(encoding="utf-8"))
    second_payload = json.loads(generation_two_path.read_text(encoding="utf-8"))
    assert {
        first_payload["workspace"]["retention"],
        second_payload["workspace"]["retention"],
    } == {"pending_cleanup", "deleted"}

    monkeypatch.setattr(
        workspace_cleanup,
        "update_workspace_retention",
        original_update,
    )
    retry_result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert retry_result.exit_code == 0, retry_result.output
    assert "Removed 0 workspace path(s)" in retry_result.output
    first_payload = json.loads(state_path.read_text(encoding="utf-8"))
    second_payload = json.loads(generation_two_path.read_text(encoding="utf-8"))
    assert first_payload["status"] == "failed"
    assert second_payload["status"] == "cancelled"
    assert first_payload["workspace"]["retention"] == "deleted"
    assert second_payload["workspace"]["retention"] == "deleted"


@pytest.mark.parametrize("retention", ["pending_cleanup", "deleted"])
def test_cleanup_workspaces_failed_filter_reconciles_absent_workspace_refs(
    tmp_path: Path,
    retention: str,
) -> None:
    project_root, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
        run_status="failed",
    )
    state_path = (
        project_root / ".crewplane/execution-stages/run-1/node/workspace-state.json"
    )
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["workspace"]["retention"] = retention
    payload["ref_publication"]["phase"] = "published"
    destinations = payload["ref_publication"]["destinations"]
    ref_targets = {
        destination["name"]: destination["target_oid"]
        for destination in destinations.values()
    }
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    for ref_name, target_oid in ref_targets.items():
        _git(project_root, "update-ref", ref_name, target_oid)
    _git(
        project_root,
        "worktree",
        "remove",
        "--force",
        (workspace_path / "checkout").as_posix(),
    )
    workspace_path.rmdir()

    result = CliRunner().invoke(
        app,
        [
            "cleanup",
            "workspaces",
            "--config",
            config_path.as_posix(),
            "--failed",
            "--yes",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert "Removed 0 workspace path(s)" in result.output
    for ref_name in ref_targets:
        assert _git(project_root, "for-each-ref", ref_name).strip() == ""
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["workspace"]["retention"] == "deleted"
    assert persisted["ref_publication"]["phase"] == "removed"


def test_cleanup_workspaces_does_not_reconcile_absent_state_for_active_run(
    tmp_path: Path,
) -> None:
    project_root, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    state_dir = project_root / ".crewplane"
    state_path = state_dir / "execution-stages/run-1/node/workspace-state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["workspace"]["retention"] = "pending_cleanup"
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    write_run_manifest(
        state_dir,
        make_run_manifest(
            run_id="run-1",
            run_key_name="run-1",
            status="running",
        ),
    )
    _git(
        project_root,
        "worktree",
        "remove",
        "--force",
        (workspace_path / "checkout").as_posix(),
    )
    workspace_path.rmdir()

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Removed 0 workspace path(s)" in result.output
    retained = json.loads(state_path.read_text(encoding="utf-8"))
    assert retained["workspace"]["retention"] == "pending_cleanup"


def test_cleanup_workspaces_retains_duplicate_generation_claims(
    tmp_path: Path,
) -> None:
    project_root, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    state_path = (
        project_root / ".crewplane/execution-stages/run-1/node/workspace-state.json"
    )
    duplicate_path = state_path.with_name("workspace-reuse-claim-duplicate.json")
    duplicate_path.write_bytes(state_path.read_bytes())

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "duplicate generation claims" in result.output
    assert workspace_path.exists()


def test_cleanup_workspaces_orphans_retains_corrupt_workspace_claim(
    tmp_path: Path,
) -> None:
    project_root, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    state_path = (
        project_root / ".crewplane/execution-stages/run-1/node/workspace-state.json"
    )
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["execution"].pop("workspace_path")
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "cleanup",
            "workspaces",
            "--config",
            config_path.as_posix(),
            "--orphans",
            "--yes",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert "evidence for the run is malformed" in result.output
    assert workspace_path.exists()


def test_cleanup_workspaces_orphans_retains_claim_omitted_from_plan(
    tmp_path: Path,
) -> None:
    project_root, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    plan_path = (
        project_root / ".crewplane/execution-stages/run-1/preflight/execution-plan.json"
    )
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    payload["nodes"] = []
    plan_path.write_text(json.dumps(payload), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "cleanup",
            "workspaces",
            "--config",
            config_path.as_posix(),
            "--orphans",
            "--yes",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert "Removed 0 workspace path(s)" in result.output
    assert "evidence for the run is malformed" in result.output
    assert workspace_path.exists()


def test_cleanup_workspaces_retains_worktree_with_wrong_git_backlink(
    tmp_path: Path,
) -> None:
    project_root, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    git_dir = Path(_git(workspace_path / "checkout", "rev-parse", "--git-dir").strip())
    (git_dir / "gitdir").write_text(
        (tmp_path / "other" / ".git").as_posix(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "identity or disposal safety is unverifiable" in result.output
    assert workspace_path.exists()


def test_cleanup_workspaces_yes_ignores_symlink_workspace_candidates(
    tmp_path: Path,
) -> None:
    _, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
        create_workspace=False,
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    workspace_path.parent.mkdir(parents=True)
    try:
        workspace_path.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Removed 0 workspace path(s)" in result.output
    assert workspace_path.is_symlink()
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_cleanup_workspaces_all_projects_allows_non_git_project(
    tmp_path: Path,
) -> None:
    _, config_path, workspace_path = _cleanup_project(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "cleanup",
            "workspaces",
            "--config",
            config_path.as_posix(),
            "--all-projects",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "cannot verify cross-project run ownership or activity" in result.output
    assert "Would remove 1 workspace path(s)" in result.output
    assert "status=unknown" in result.output
    assert "status=orphan" not in result.output
    assert workspace_path.exists()


def test_cleanup_workspaces_all_projects_yes_is_explicit_global_override(
    tmp_path: Path,
) -> None:
    _, config_path, workspace_path = _cleanup_project(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "cleanup",
            "workspaces",
            "--config",
            config_path.as_posix(),
            "--all-projects",
            "--yes",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "cannot verify cross-project run ownership or activity" in result.output
    assert "Removed 1 workspace path(s)" in result.output
    assert not workspace_path.exists()


def test_cleanup_workspaces_all_projects_rejects_orphan_filter(
    tmp_path: Path,
) -> None:
    _, config_path, workspace_path = _cleanup_project(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "cleanup",
            "workspaces",
            "--config",
            config_path.as_posix(),
            "--all-projects",
            "--orphans",
            "--yes",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "--all-projects cannot be combined" in result.output
    assert workspace_path.exists()


def test_cleanup_workspaces_default_requires_git_project(tmp_path: Path) -> None:
    _, config_path, _ = _cleanup_project(tmp_path)

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix()],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "Use --all-projects" in " ".join(result.output.split())


def test_cleanup_workspaces_rejects_relative_cache_root(tmp_path: Path) -> None:
    _, config_path, workspace_path = _cleanup_project(tmp_path, initialize_git=True)
    config_text = config_path.read_text(encoding="utf-8")
    cache_root = workspace_path.parents[3]
    config_path.write_text(
        config_text.replace(cache_root.as_posix(), "relative-cache"),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "cache_root must be absolute" in result.output
    assert workspace_path.exists()


def test_cleanup_workspaces_rejects_dangling_cache_root_symlink(
    tmp_path: Path,
) -> None:
    _, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
        create_workspace=False,
    )
    cache_root = workspace_path.parents[3]
    try:
        cache_root.symlink_to(
            tmp_path / "missing-cache-target", target_is_directory=True
        )
    except OSError:
        pytest.skip("symlink creation is unavailable")

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "Workspace cache root must not be a symlink" in result.output
    assert cache_root.is_symlink()


def test_cleanup_workspaces_rejects_project_cache_root(tmp_path: Path) -> None:
    project_root, config_path, workspace_path = _cleanup_project(
        tmp_path,
        initialize_git=True,
    )
    config_text = config_path.read_text(encoding="utf-8")
    cache_root = workspace_path.parents[3]
    config_path.write_text(
        config_text.replace(cache_root.as_posix(), project_root.as_posix()),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "must not overlap" in result.output
    assert project_root.exists()


def _resolve_workspace_cleanup_context(
    config_path: Path,
    orphans: bool = False,
) -> cleanup_cli.WorkspaceCleanupContext:
    return cleanup_cli.resolve_cleanup_workspace_context(
        console=Console(),
        config_file=config_path,
        successful=False,
        failed=False,
        cancelled=False,
        all_projects=False,
        run_key_name=None,
        older_than=None,
        orphans=orphans,
    )


def _write_cleanup_node_manifest(project_root: Path, run_key_name: str) -> None:
    node_manifest_path = (
        project_root
        / ".crewplane/execution-stages"
        / run_key_name
        / "manifests/nodes/node.json"
    )
    node_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    node_manifest_path.write_text("{}", encoding="utf-8")


def _copy_cleanup_run(project_root: Path, run_key_name: str) -> None:
    source_run_dir = project_root / ".crewplane/execution-stages/run-1"
    target_run_dir = project_root / ".crewplane/execution-stages" / run_key_name
    manifest = json.loads(
        (source_run_dir / "manifests/run.json").read_text(encoding="utf-8")
    )
    manifest.update({"run_id": run_key_name, "run_key_name": run_key_name})
    manifest_path = target_run_dir / "manifests/run.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    plan = json.loads(
        (source_run_dir / "preflight/execution-plan.json").read_text(encoding="utf-8")
    )
    plan.update({"run_id": run_key_name, "run_key_name": run_key_name})
    plan_path = target_run_dir / "preflight/execution-plan.json"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    _write_cleanup_node_manifest(project_root, run_key_name)


def _removed_cleanup_result(
    cache_root: Path,
    *run_key_names: str,
) -> workspace_cleanup.WorkspaceCleanupResult:
    return workspace_cleanup.WorkspaceCleanupResult(
        cache_root=cache_root,
        entries=tuple(
            workspace_cleanup.WorkspaceCleanupEntry(
                path=cache_root / run_key_name,
                run_key_name=run_key_name,
                size_bytes=0,
                removed=True,
                status="succeeded",
                orphan=False,
            )
            for run_key_name in run_key_names
        ),
    )


def _workspace_manifest_content(manifest_kind: str) -> str:
    if manifest_kind == "invalid":
        return "{}"
    if manifest_kind == "mismatched":
        return make_run_manifest(
            run_id="other-run",
            run_key_name="other-run",
            status="succeeded",
        ).model_dump_json()
    if manifest_kind == "running":
        return make_run_manifest(
            run_id="run-1",
            run_key_name="run-1",
            status="running",
        ).model_dump_json()
    raise AssertionError(f"Unsupported manifest kind: {manifest_kind}")


def _cleanup_project(
    tmp_path: Path,
    initialize_git: bool = False,
    create_workspace: bool = True,
    run_status: str = "succeeded",
) -> tuple[Path, Path, Path]:
    project_root = tmp_path / "project"
    state_dir = project_root / ".crewplane"
    cache_root = tmp_path / "workspace-cache"
    project_root.mkdir()
    if initialize_git:
        _git(project_root, "init")
        _git(project_root, "config", "user.name", "Crewplane Test")
        _git(project_root, "config", "user.email", "crewplane-test@example.invalid")
        (project_root / "README.md").write_text("ready\n", encoding="utf-8")
        _git(project_root, "add", ".")
        _git(project_root, "commit", "-m", "initial")
    repo_id = (
        cleanup_repository_id(project_root, all_projects=False)
        if initialize_git
        else "repo-1"
    )
    workspace_path = cache_root / "workspaces" / repo_id / "run-1" / "node-round1"
    if create_workspace:
        if initialize_git:
            workspace_path.parent.mkdir(parents=True)
            checkout_root = workspace_path / "checkout"
            _git(
                project_root,
                "worktree",
                "add",
                "--detach",
                checkout_root.as_posix(),
                "HEAD",
            )
            (checkout_root / "file.txt").write_text("payload", encoding="utf-8")
        else:
            workspace_path.mkdir(parents=True)
            (workspace_path / "file.txt").write_text("payload", encoding="utf-8")
    state_dir.mkdir(parents=True)
    if create_workspace:
        state_path = (
            state_dir / "execution-stages" / "run-1" / "node" / "workspace-state.json"
        )
        state_path.parent.mkdir(parents=True)
        state_payload = (
            _cleanup_workspace_state(
                project_root,
                workspace_path,
                repo_id,
                run_status,
            )
            if initialize_git
            else {
                "run_key_name": "run-1",
                "status": run_status,
                "workspace": {"cache_key": workspace_path.name},
            }
        )
        state_path.write_text(
            json.dumps(state_payload),
            encoding="utf-8",
        )
        _write_cleanup_plan_and_manifest(
            state_dir,
            project_root,
            run_status,
        )
    config_path = state_dir / "config.yml"
    config_path.write_text(
        "\n".join(
            [
                f'version: "{SCHEMA_VERSION}"',
                "agents:",
                "  alpha:",
                '    cli_cmd: ["mock"]',
                '    default_model: "test"',
                "settings:",
                "  workspace:",
                "    enabled: true",
                f'    cache_root: "{cache_root.as_posix()}"',
                "    cleanup_on_success: true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return project_root, config_path, workspace_path


def _write_cleanup_plan_and_manifest(
    state_dir: Path,
    project_root: Path,
    run_status: str,
) -> None:
    manifest = make_run_manifest(
        run_id="run-1",
        run_key_name="run-1",
        status=run_status,
    )
    run_dir = state_dir / "execution-stages" / "run-1"
    payload = make_plan().model_dump(mode="json")
    payload.update(
        {
            "run_id": manifest.run_id,
            "run_key_name": manifest.run_key_name,
            "project_root": project_root.as_posix(),
            "context_root": run_dir.as_posix(),
            "manifest_root": (run_dir / "manifests").as_posix(),
            "execution_order": ["node"],
            "nodes": [payload["nodes"][0]],
            "render_plans": [payload["render_plans"][0]],
            "dependency_graph": [],
        }
    )
    node = payload["nodes"][0]
    assert isinstance(node, dict)
    node["id"] = "node"
    node["render_plan_id"] = "node"
    node["dependencies"] = []
    node["workspace_policy"] = workspace_selection_record(
        kind="worktree",
        logical_name="primary",
        lineage_producer=True,
    ).model_dump(mode="json")
    contract = node["artifact_contract"]
    assert isinstance(contract, dict)
    contract.update(
        {
            "stage_path": "node",
            "output_path": "node/output.md",
            "findings_path": None,
            "log_path": "node/logs",
            "result_path": "node/output.md",
        }
    )
    render_plan = payload["render_plans"][0]
    assert isinstance(render_plan, dict)
    render_plan["render_plan_id"] = "node"
    render_plan["node_id"] = "node"
    plan = PreflightExecutionPlan.model_validate(payload)
    plan_path = run_dir / manifest.preflight_plan_path
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(plan.model_dump_json(), encoding="utf-8")
    write_run_manifest(state_dir, manifest)


def _cleanup_workspace_state(
    project_root: Path,
    workspace_path: Path,
    repo_id: str,
    status: str,
) -> dict[str, object]:
    manifest = make_run_manifest(
        run_id="run-1",
        run_key_name="run-1",
        status=status,
    )
    head = _git(project_root, "rev-parse", "HEAD^{commit}").strip()
    tree = _git(project_root, "rev-parse", "HEAD^{tree}").strip()
    checkout_root = workspace_path / "checkout"
    git_dir_text = _git(checkout_root, "rev-parse", "--git-dir").strip()
    git_dir_path = Path(git_dir_text)
    git_dir = (
        git_dir_path if git_dir_path.is_absolute() else checkout_root / git_dir_path
    ).resolve()
    common_git_dir = (
        project_root / _git(project_root, "rev-parse", "--git-common-dir").strip()
    ).resolve()
    invocation_slug = "node-alpha-round1"
    ref_root = f"refs/crewplane/runs/run-1/node/{invocation_slug}"
    candidate_ref = f"{ref_root}/candidate"
    result_ref = f"{ref_root}/result"
    return {
        "version": SCHEMA_VERSION,
        "run_id": "run-1",
        "run_key_name": "run-1",
        "workflow_name": manifest.workflow_name,
        "workflow_signature": manifest.workflow_signature,
        "node_id": "node",
        "task_id": "alpha",
        "provider": "alpha",
        "role": "executor",
        "round_num": 1,
        "audit_round_num": None,
        "status": status,
        "workspace_kind": "worktree",
        "logical_worktree_name": "primary",
        "clean_start": "strict",
        "worktree_contract": WORKTREE_CONTRACT_PAYLOAD,
        "git": {
            "object_format": _git(
                project_root, "rev-parse", "--show-object-format=storage"
            ).strip(),
            "repo_id": repo_id,
            "run_base_commit": head,
            "source_tree": tree,
            "git_top_level": project_root.as_posix(),
            "active_git_dir": git_dir.as_posix(),
            "common_git_dir": common_git_dir.as_posix(),
        },
        "source": {
            "kind": "project",
            "node_id": None,
            "commit": head,
            "tree": tree,
            "candidate_sequence": None,
        },
        "invocation_source": {
            "source_kind": "project",
            "source_node_id": None,
            "source_commit": head,
            "source_tree": tree,
            "candidate_sequence": None,
        },
        "workspace": {
            "path": workspace_path.as_posix(),
            "effective_cwd": checkout_root.as_posix(),
            "materialization": "worktree_checkout",
            "writable": True,
            "lineage_producer": True,
            "retention": "retained",
            "retained_reason": None,
            "project_root_relative_path": ".",
            "reuse_generation": 1,
            "cache_key": workspace_path.name,
        },
        "execution": {
            "cache_root": workspace_path.parents[3].as_posix(),
            "workspace_path": workspace_path.as_posix(),
            "checkout_root": checkout_root.as_posix(),
            "effective_cwd": checkout_root.as_posix(),
            "worktree_git_dir": git_dir.as_posix(),
        },
        "process_drain": {"status": "confirmed"},
        "result": {
            "candidate_commit": head,
            "result_commit": head,
            "candidate_tree": tree,
            "result_tree": tree,
            "changed_path_count": 0,
            "final_head": head,
        },
        "bundle": {
            "path": "node/workspace-bundles/node-alpha-round1.bundle",
            "sha256": "f" * 64,
            "size_bytes": 0,
            "verified": True,
        },
        "refs": {"candidate": candidate_ref, "result": result_ref},
        "ref_publication": {
            "phase": "removed",
            "repository_id": repo_id,
            "run_id": "run-1",
            "run_key_name": "run-1",
            "node_id": "node",
            "task_id": "alpha",
            "role": "executor",
            "round_num": 1,
            "audit_round_num": None,
            "destinations": {
                "candidate": {
                    "name": candidate_ref,
                    "target_oid": head,
                    "expected_old_oid": None,
                },
                "result": {
                    "name": result_ref,
                    "target_oid": head,
                    "expected_old_oid": None,
                },
            },
        },
    }


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo.as_posix(), *args],
        check=True,
        capture_output=True,
    )
    return result.stdout.decode("utf-8", errors="replace")
