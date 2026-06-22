import json
from pathlib import Path

from orchestrator_cli.observability.run_summary.models import (
    WorkspaceInvocationSummary,
    WorkspacePlanSummary,
)
from orchestrator_cli.observability.run_summary.workspace import (
    merge_workspace_invocations,
    workspace_plan_summary,
    workspace_state_summary,
)
from orchestrator_cli.observability.run_summary.workspace_readers import (
    workspace_source_summary,
)
from orchestrator_cli.version import SCHEMA_VERSION


def test_workspace_plan_summary_reads_descriptor_sections(tmp_path: Path) -> None:
    _write_workspace_manifest(
        tmp_path,
        {
            "worktree_contract": {
                "mode": "blob_exact",
                "schema_version": SCHEMA_VERSION,
            },
            "source": {
                "run_base_commit": "a" * 40,
                "source_tree": "b" * 40,
                "object_format": "sha1",
                "clean_start": "tracked_only",
            },
            "invoker": {
                "implementation": "cli",
                "launch_mode": "runtime_command_runner",
                "controlled_child_environment": True,
            },
            "rendered_files": {
                "locator_count": 3,
                "project_initial": 2,
                "runtime_dynamic": 1,
            },
            "cleanup": {
                "cleanup_on_success": False,
                "cache_root_configured": True,
            },
            "nodes": [{"node_id": "node.a"}, {"node_id": "node.b"}],
        },
    )

    assert workspace_plan_summary(tmp_path) == WorkspacePlanSummary(
        worktree_contract_mode="blob_exact",
        worktree_contract_schema_version=SCHEMA_VERSION,
        source_commit="a" * 40,
        source_tree="b" * 40,
        object_format="sha1",
        clean_start="tracked_only",
        invoker_implementation="cli",
        invoker_launch_mode="runtime_command_runner",
        invoker_controlled_child_environment=True,
        rendered_locator_count=3,
        rendered_project_initial_count=2,
        rendered_runtime_dynamic_count=1,
        cleanup_on_success=False,
        cache_root_configured=True,
        planned_workspace_node_count=2,
    )


def test_workspace_plan_summary_ignores_invalid_descriptor_sections(
    tmp_path: Path,
) -> None:
    _write_workspace_manifest(
        tmp_path,
        {
            "worktree_contract": {"mode": "blob_exact"},
            "source": "not-a-section",
            "invoker": [],
            "rendered_files": None,
            "cleanup": False,
            "nodes": {"node_id": "node.a"},
        },
    )

    assert workspace_plan_summary(tmp_path) == WorkspacePlanSummary(
        worktree_contract_mode="blob_exact",
        worktree_contract_schema_version=None,
        source_commit=None,
        source_tree=None,
        object_format=None,
        clean_start=None,
        invoker_implementation=None,
        invoker_launch_mode=None,
        invoker_controlled_child_environment=None,
        rendered_locator_count=None,
        rendered_project_initial_count=None,
        rendered_runtime_dynamic_count=None,
        cleanup_on_success=None,
        cache_root_configured=None,
        planned_workspace_node_count=0,
    )


def test_workspace_plan_summary_returns_none_without_descriptor(
    tmp_path: Path,
) -> None:
    assert workspace_plan_summary(tmp_path) is None


def test_workspace_state_summary_reads_workspace_state_sections(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "node.a" / "workspace-state.json"
    state_path.parent.mkdir()

    summary = workspace_state_summary(
        tmp_path,
        state_path,
        {
            "node_id": "node.a",
            "task_id": "alpha_executor_0",
            "audit_round_num": 2,
            "round_num": 3,
            "workspace_kind": "worktree",
            "logical_worktree_name": "implementation",
            "status": "succeeded",
            "invocation_source": {
                "source_kind": "node",
                "source_node_id": "upstream.node",
                "source_commit": "a" * 40,
                "source_tree": "b" * 40,
            },
            "worktree_contract": {
                "mode": "blob_exact",
                "schema_version": SCHEMA_VERSION,
            },
            "workspace": {
                "writable": True,
                "lineage_producer": True,
                "materialization": "worktree_checkout",
                "retention": "deleted",
            },
            "child_process_environment": {
                "required": True,
                "applied": False,
            },
            "result": {
                "result_commit": "c" * 40,
                "result_tree": "d" * 40,
                "candidate_commit": "e" * 40,
                "candidate_tree": "f" * 40,
                "final_head": "1" * 40,
                "changed_path_count": 2,
                "snapshot_drift_discarded": True,
                "changed_paths": ["dist/app.js", "coverage.xml"],
                "changed_paths_truncated": False,
            },
            "bundle": {
                "path": "node.a/workspace-bundles/alpha.bundle",
                "size_bytes": 123,
                "verified": True,
            },
            "rendered_workspace_files": [{"path": "README.md"}],
            "diagnostics": [{"level": "warning"}],
            "execution": {
                "cache_root": "/tmp/orchestrator-cache",
                "workspace_path": "/tmp/orchestrator-cache/workspace",
                "checkout_root": "/tmp/orchestrator-cache/workspace/checkout",
                "effective_cwd": "/tmp/orchestrator-cache/workspace/checkout",
                "checkout_size_bytes": 4096,
                "provisioning_duration_seconds": 1.5,
            },
            "setup": {
                "profile_name": "bootstrap",
                "status": "succeeded",
                "duration_seconds": 2,
                "failure_message": "not used",
                "commands": [{"argv": ["uv", "sync"]}],
                "log_path": "workspace-setup/setup.log",
                "metadata_path": "workspace-setup/setup.json",
            },
            "reuse": {
                "strategy": "incremental_reset",
                "reused": True,
                "fallback": False,
                "fallback_reason": "not used",
                "previous_workspace_state": "previous/workspace-state.json",
            },
            "branch_export": {
                "status": "fulfilled",
                "operation": "created",
                "branch_name": "feature/generated",
                "branch_ref": "refs/heads/feature/generated",
                "record_artifact": "workspace-exports/implementation.json",
                "failure_message": "not used",
            },
        },
    )

    assert summary is not None
    assert summary.node_id == "node.a"
    assert summary.task_id == "alpha_executor_0"
    assert summary.audit_round_num == 2
    assert summary.round_num == 3
    assert summary.workspace_kind == "worktree"
    assert summary.logical_worktree_name == "implementation"
    assert summary.state_path == "node.a/workspace-state.json"
    assert summary.source.kind == "node"
    assert summary.source.node_id == "upstream.node"
    assert summary.source.commit == "a" * 40
    assert summary.source.worktree_contract_mode == "blob_exact"
    assert summary.writable is True
    assert summary.lineage_producer is True
    assert summary.child_environment_required is True
    assert summary.child_environment_applied is False
    assert summary.materialization == "worktree_checkout"
    assert summary.retention == "deleted"
    assert summary.result_commit == "c" * 40
    assert summary.candidate_tree == "f" * 40
    assert summary.changed_path_count == 2
    assert summary.bundle_path == "node.a/workspace-bundles/alpha.bundle"
    assert summary.bundle_size_bytes == 123
    assert summary.bundle_verified is True
    assert summary.rendered_file_count == 1
    assert summary.diagnostic_count == 1
    assert summary.execution.checkout_size_bytes == 4096
    assert summary.execution.provisioning_duration_seconds == 1.5
    assert summary.setup.profile_name == "bootstrap"
    assert summary.setup.command_count == 1
    assert summary.setup.log_path == "node.a/workspace-setup/setup.log"
    assert summary.reuse.reset_verification == "verified"
    assert summary.branch_export.operation == "created"
    assert summary.snapshot_drift_discarded is True
    assert summary.snapshot_changed_paths_reported == 2
    assert summary.snapshot_changed_paths_truncated is False


def test_workspace_state_summary_ignores_invalid_optional_sections(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "node.a" / "workspace-state.json"

    summary = workspace_state_summary(
        tmp_path,
        state_path,
        {
            "node_id": "node.a",
            "task_id": "alpha_executor_0",
            "round_num": 1,
            "workspace": "not-a-section",
            "result": [],
            "bundle": False,
            "child_process_environment": "not-a-section",
            "worktree_contract": None,
            "execution": "not-a-section",
            "setup": [],
            "reuse": False,
            "branch_export": "not-a-section",
            "diagnostics": "not-a-list",
            "rendered_workspace_files": "not-a-list",
        },
    )

    assert summary is not None
    assert summary.source.kind is None
    assert summary.writable is None
    assert summary.result_commit is None
    assert summary.bundle_path is None
    assert summary.child_environment_required is None
    assert summary.execution.checkout_size_bytes is None
    assert summary.setup.status is None
    assert summary.reuse.strategy is None
    assert summary.branch_export.status is None
    assert summary.rendered_file_count is None
    assert summary.diagnostic_count is None
    assert summary.snapshot_changed_paths_reported is None


def test_workspace_source_summary_prefers_explicit_source_field_names() -> None:
    source_summary = workspace_source_summary(
        {
            "source_kind": "node",
            "kind": "project",
            "source_node_id": "preferred.node",
            "node_id": "fallback.node",
            "source_commit": "a" * 40,
            "commit": "b" * 40,
            "source_tree": "c" * 40,
            "tree": "d" * 40,
        },
        {"mode": "blob_exact", "schema_version": SCHEMA_VERSION},
    )

    assert source_summary.kind == "node"
    assert source_summary.node_id == "preferred.node"
    assert source_summary.commit == "a" * 40
    assert source_summary.tree == "c" * 40
    assert source_summary.worktree_contract_mode == "blob_exact"
    assert source_summary.worktree_contract_schema_version == SCHEMA_VERSION


def test_workspace_source_summary_uses_fallback_source_field_names() -> None:
    source_summary = workspace_source_summary(
        {
            "source_kind": "",
            "kind": "project",
            "source_node_id": None,
            "node_id": "fallback.node",
            "source_commit": False,
            "commit": "b" * 40,
            "source_tree": 123,
            "tree": "d" * 40,
        },
        None,
    )

    assert source_summary.kind == "project"
    assert source_summary.node_id == "fallback.node"
    assert source_summary.commit == "b" * 40
    assert source_summary.tree == "d" * 40
    assert source_summary.worktree_contract_mode is None
    assert source_summary.worktree_contract_schema_version is None


def test_workspace_summaries_reject_bool_values_for_integer_fields(
    tmp_path: Path,
) -> None:
    _write_workspace_manifest(
        tmp_path,
        {
            "rendered_files": {
                "locator_count": True,
                "project_initial": False,
                "runtime_dynamic": True,
            },
        },
    )

    plan_summary = workspace_plan_summary(tmp_path)
    assert plan_summary is not None
    assert plan_summary.rendered_locator_count is None
    assert plan_summary.rendered_project_initial_count is None
    assert plan_summary.rendered_runtime_dynamic_count is None

    state_summary = workspace_state_summary(
        tmp_path,
        tmp_path / "node.a" / "workspace-state.json",
        {
            "node_id": "node.a",
            "result": {"changed_path_count": True},
            "bundle": {"size_bytes": False},
            "execution": {"checkout_size_bytes": True},
        },
    )

    assert state_summary is not None
    assert state_summary.changed_path_count is None
    assert state_summary.bundle_size_bytes is None
    assert state_summary.execution.checkout_size_bytes is None


def test_merge_workspace_invocations_keeps_event_state_path_and_state_facts(
    tmp_path: Path,
) -> None:
    event_invocation = _workspace_invocation(
        state_path=str(tmp_path / "node.a" / "workspace-state.json"),
        status="failed",
        writable=None,
        lineage_producer=None,
        child_environment_required=True,
        child_environment_applied=None,
    )
    state_invocation = _workspace_invocation(
        state_path="node.a/workspace-state.json",
        status="running",
        writable=True,
        lineage_producer=True,
        child_environment_required=None,
        child_environment_applied=True,
    )

    merged = merge_workspace_invocations(
        tmp_path,
        (event_invocation,),
        (state_invocation,),
    )

    assert len(merged) == 1
    assert merged[0].status == "failed"
    assert merged[0].state_path == "node.a/workspace-state.json"
    assert merged[0].writable is True
    assert merged[0].lineage_producer is True
    assert merged[0].child_environment_required is True
    assert merged[0].child_environment_applied is True


def _write_workspace_manifest(stages_dir: Path, workspace: object) -> None:
    manifest_dir = stages_dir / "preflight"
    manifest_dir.mkdir()
    (manifest_dir / "manifest.json").write_text(
        json.dumps({"workspace": workspace}),
        encoding="utf-8",
    )


def _workspace_invocation(
    state_path: str,
    status: str,
    writable: bool | None,
    lineage_producer: bool | None,
    child_environment_required: bool | None,
    child_environment_applied: bool | None,
) -> WorkspaceInvocationSummary:
    return WorkspaceInvocationSummary(
        node_id="node.a",
        task_id="alpha_executor_0",
        audit_round_num=None,
        round_num=1,
        workspace_kind="worktree",
        logical_worktree_name="implementation",
        status=status,
        state_path=state_path,
        writable=writable,
        lineage_producer=lineage_producer,
        child_environment_required=child_environment_required,
        child_environment_applied=child_environment_applied,
    )
