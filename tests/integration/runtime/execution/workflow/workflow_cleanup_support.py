from __future__ import annotations

from datetime import UTC, datetime

from crewplane.artifacts import OutputManager
from crewplane.core.preflight.models import (
    ArtifactContract,
    PreflightExecutionNode,
    PreflightExecutionPlan,
)
from crewplane.version import SCHEMA_VERSION


def empty_cleanup_plan(output: OutputManager) -> PreflightExecutionPlan:
    return PreflightExecutionPlan(
        plan_schema_version=SCHEMA_VERSION,
        run_id=output.run_id,
        run_key_name=output.run_key_name,
        project_root=output.base_dir.as_posix(),
        context_root=output.stages_dir.as_posix(),
        manifest_root=(output.stages_dir / "manifests").as_posix(),
        created_at=datetime.now(UTC).isoformat(),
        workflow_name="workflow",
        workflow_signature="workflow-signature",
        execution_order=[],
        nodes=[],
        render_plans=[],
        static_resources=[],
        workspace_file_locators=[],
        token_catalog=[],
        dependency_graph=[],
        runtime_config_snapshot={"schema_version": SCHEMA_VERSION},
        effective_runtime_config_signature="runtime-signature",
        fingerprint_metadata={"payload_version": "1"},
    )


def single_node_cleanup_plan(output: OutputManager) -> PreflightExecutionPlan:
    plan = empty_cleanup_plan(output)
    node = PreflightExecutionNode(
        id="input",
        mode="input",
        artifact_contract=ArtifactContract(output_path="input.md"),
    )
    return plan.model_copy(
        update={
            "execution_order": ["input"],
            "nodes": [node],
        }
    )
