from __future__ import annotations

from orchestrator_cli.cli.run.manifest import build_run_manifest_from_plan
from orchestrator_cli.core.preflight.source import PreflightWorkflowSource
from orchestrator_cli.core.workflow_models import WorkflowPlan
from tests.helpers.resume import make_snapshot_workspace_plan


def test_run_manifest_records_workspace_descriptor() -> None:
    plan = make_snapshot_workspace_plan()
    plan = plan.model_copy(
        update={
            "runtime_config_snapshot": {
                **plan.runtime_config_snapshot,
                "workspace": {
                    "enabled": True,
                    "cache_root": "/tmp/orchestrator-workspace-cache",
                    "cleanup_on_success": True,
                },
            }
        }
    )
    source = PreflightWorkflowSource.from_workflow(
        WorkflowPlan(name=plan.workflow_name, nodes=[])
    )

    manifest = build_run_manifest_from_plan(
        plan,
        source,
        workflow_identity=".orchestrator/workflows/workflow.task.md",
    )

    assert manifest.workspace is not None
    assert manifest.workspace["enabled"] is True
    assert manifest.workspace["worktree_contract"]["mode"] == "blob_exact"
    assert manifest.workspace["source"]["run_base_commit"] == "a" * 40
    assert manifest.workspace["cleanup"]["cleanup_on_success"] is True
    assert manifest.workspace["rendered_files"]["locator_count"] == 1
    assert manifest.workspace["nodes"][0]["materialization"] == "snapshot_checkout"
    assert manifest.workspace["nodes"][0]["result"]["capture"] == "static_file"
