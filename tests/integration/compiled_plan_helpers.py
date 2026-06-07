from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console

from orchestrator_cli.architecture.ports.runtime import RuntimeComponents
from orchestrator_cli.bootstrap import build_runtime_config_snapshot
from orchestrator_cli.core.config import Config
from orchestrator_cli.core.preflight import (
    PreflightCompileOptions,
    PreflightExecutionPlan,
    PreflightWorkflowSource,
    compile_preflight_preview,
)
from orchestrator_cli.core.preflight.secrets import SecretContext
from orchestrator_cli.core.workflow_models import WorkflowPlan


def compile_plan_for_components(
    config: Config,
    workflow: WorkflowPlan,
    components: RuntimeComponents,
    project_root: Path,
    composed_workflow: dict[str, Any] | None = None,
) -> tuple[PreflightExecutionPlan, SecretContext]:
    output = components.artifact_store
    snapshot = build_runtime_config_snapshot(
        config=config,
        workflow_schema_version=workflow.schema_version,
        console=Console(file=None),
        no_live=True,
    )
    preview = compile_preflight_preview(
        source=PreflightWorkflowSource.from_workflow(
            workflow,
            workflow_content="test workflow",
            composed_workflow=composed_workflow
            or {
                "schema_version": workflow.schema_version,
                "name": workflow.name,
                "description": workflow.description,
                "inputs": dict(workflow.inputs),
                "nodes": [],
            },
        ),
        config=config,
        runtime_snapshot=snapshot.snapshot,
        options=PreflightCompileOptions(
            project_root=project_root,
            orchestrator_dir=output.base_dir,
            fingerprint_key_policy="read_only",
        ),
    )
    if preview.diagnostics:
        diagnostics = "; ".join(
            f"{diagnostic.code}: {diagnostic.message}"
            for diagnostic in preview.diagnostics
        )
        raise AssertionError(f"Unexpected preflight diagnostics: {diagnostics}")
    plan = PreflightExecutionPlan.from_preview(
        preview=preview,
        run_id=output.run_id,
        run_key_name=output.stages_dir.name,
        context_root=output.stages_dir.as_posix(),
        manifest_root=(output.stages_dir / "manifests").as_posix(),
        created_at=datetime(2026, 6, 3),
    )
    for content_ref, payload in preview.static_file_payloads.items():
        output.write_preflight_static_file(content_ref, payload)
    return plan, preview.secret_context
