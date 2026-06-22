from __future__ import annotations

from datetime import datetime

from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.core.preflight import (
    PREFLIGHT_STATUS_SUCCEEDED,
    PreflightExecutionPlan,
)
from orchestrator_cli.core.preflight.workspace.observability import (
    workspace_observability_descriptor,
)


def write_preflight_success_artifacts(
    output: ArtifactStorePort,
    plan: PreflightExecutionPlan,
) -> None:
    created_at = datetime.now().isoformat()
    workspace_descriptor = workspace_observability_descriptor(plan)
    metadata_payload = {
        "created_at": created_at,
        "run_id": output.run_id,
        "run_key_name": output.run_key_name,
        "workflow_name": plan.workflow_name,
        "workflow_signature": plan.workflow_signature,
    }
    manifest_payload = {
        **metadata_payload,
        "dependency_count": len(plan.dependency_graph),
        "render_plan_count": len(plan.render_plans),
        "static_resource_count": len(plan.static_resources),
        "status": PREFLIGHT_STATUS_SUCCEEDED,
        "token_count": len(plan.token_catalog),
        "value_fingerprint_count": len(plan.value_fingerprints),
    }
    if workspace_descriptor is not None:
        manifest_payload["workspace"] = workspace_descriptor
    output.write_preflight_metadata(metadata_payload)
    output.write_preflight_manifest(manifest_payload)
    output.write_preflight_render_plan(plan.render_plans)
    output.write_preflight_json("static-resources.json", plan.static_resources)
    output.write_preflight_json("token-catalog.json", plan.token_catalog)
    output.write_preflight_json("dependency-graph.json", plan.dependency_graph)
    if plan.workspace_file_locators:
        output.write_preflight_json(
            "workspace-file-locators.json",
            plan.workspace_file_locators,
        )
    if plan.workspace_source is not None:
        output.write_preflight_json("workspace-source.json", plan.workspace_source)
    output.write_preflight_execution_bundle(
        {
            "dependency_graph": plan.dependency_graph,
            "render_plans": plan.render_plans,
            "static_resources": plan.static_resources,
            "workspace_file_locators": plan.workspace_file_locators,
            "workspace_source": plan.workspace_source,
            "token_catalog": plan.token_catalog,
            "value_fingerprints": plan.value_fingerprints,
        }
    )
    output.write_preflight_json(
        "runtime-config-snapshot.json",
        plan.runtime_config_snapshot,
    )
    output.write_preflight_summary(
        "\n".join(preflight_success_summary_lines(plan)) + "\n"
    )


def preflight_success_summary_lines(plan: PreflightExecutionPlan) -> list[str]:
    return [
        "# Preflight Summary",
        "",
        f"- Workflow: {plan.workflow_name}",
        f"- Run Key: {plan.run_key_name}",
        f"- Workflow Signature: {plan.workflow_signature}",
        f"- Effective Runtime Config Signature: {plan.effective_runtime_config_signature}",
        "- Execution Plan: preflight/execution-plan.json",
        "- Execution Bundle: preflight/execution-bundle.json",
        f"- Nodes: {len(plan.nodes)}",
        f"- Render Plans: {len(plan.render_plans)}",
        f"- Static Resources: {len(plan.static_resources)}",
        f"- Workspace File Locators: {len(plan.workspace_file_locators)}",
        f"- Dependency Edges: {len(plan.dependency_graph)}",
        f"- Tokens: {len(plan.token_catalog)}",
        f"- Value Fingerprints: {len(plan.value_fingerprints)}",
    ]
