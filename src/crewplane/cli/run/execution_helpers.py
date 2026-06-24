from __future__ import annotations

from datetime import datetime

import typer

from crewplane.architecture.ports import ArtifactStorePort
from crewplane.artifacts.run_history import RunHistoryRecord
from crewplane.bootstrap import RuntimeConfigSnapshotBuildResult
from crewplane.core.config import Config, Settings
from crewplane.core.preflight import (
    PreflightCompilationPreview,
    PreflightExecutionPlan,
)
from crewplane.core.preflight.diagnostics import (
    PreflightDiagnostic,
    PreflightDiagnosticCode,
    PreflightDiagnosticPhase,
)
from crewplane.core.preflight.source import PreflightWorkflowSource
from crewplane.core.preflight.workspace.observability import workspace_enabled
from crewplane.runtime.workspace.branch_export import (
    fulfill_branch_exports_from_history,
)

from .branch_export_output import print_branch_export_fulfillments
from .context import WorkflowRunContext
from .historical_summary import refresh_historical_run_summary
from .manifest import (
    build_run_manifest_from_plan,
    print_duplicate_context_message,
    write_running_run_manifest,
)
from .preflight import write_preflight_failure_artifacts
from .resume import ResumePlan


def raise_run_preflight_errors(
    context: WorkflowRunContext,
    snapshot_result: RuntimeConfigSnapshotBuildResult,
    preview: PreflightCompilationPreview,
    workflow_name: str,
) -> None:
    write_preflight_failure_artifacts(
        context=context,
        snapshot_result=snapshot_result,
        diagnostics=preview.diagnostics,
        workflow_name=workflow_name,
    )
    print_run_preflight_diagnostics(context, preview.diagnostics)
    raise typer.Exit(code=1)


def raise_for_workspace_runtime_error(
    context: WorkflowRunContext,
    snapshot_result: RuntimeConfigSnapshotBuildResult,
    config: Config,
    preview: PreflightCompilationPreview,
) -> None:
    workspace_execution_error = workspace_real_execution_error(config, preview)
    if workspace_execution_error is None:
        return
    diagnostics = [
        PreflightDiagnostic(
            code=PreflightDiagnosticCode.WORKSPACE_RUNTIME,
            phase=PreflightDiagnosticPhase.WORKTREE_CONTRACT,
            message=workspace_execution_error,
        )
    ]
    write_preflight_failure_artifacts(
        context=context,
        snapshot_result=snapshot_result,
        diagnostics=diagnostics,
        workflow_name=context.workflow.name,
    )
    print_run_preflight_diagnostics(context, diagnostics)
    raise typer.Exit(code=1)


def print_run_preflight_diagnostics(
    context: WorkflowRunContext,
    diagnostics: list[PreflightDiagnostic],
) -> None:
    for diagnostic in diagnostics:
        context.console.print(
            f"[red]Preflight {diagnostic.code}:[/] {diagnostic.message}"
        )
    if any(
        diagnostic.code == PreflightDiagnosticCode.PROVIDER_CLI
        for diagnostic in diagnostics
    ):
        context.console.print(
            "[yellow]Provider setup: docs/getting-started/provider-setup.md[/]"
        )


def handle_duplicate_skip(
    context: WorkflowRunContext,
    preview: PreflightCompilationPreview,
    resume_plan: ResumePlan,
) -> bool:
    if resume_plan.decision.kind != "skip":
        return False
    successful_run = resume_plan.decision.successful_run
    if successful_run is not None:
        fulfill_duplicate_skip_branch_exports(context, preview, successful_run)
    print_duplicate_context_message(
        context,
        successful_run.manifest.run_id if successful_run is not None else None,
    )
    return True


def workspace_real_execution_error(
    config: Config,
    preview: PreflightCompilationPreview,
) -> str | None:
    settings = config.settings if config.settings is not None else Settings()
    if not settings.workspace.enabled:
        return None
    if not workspace_enabled(preview):
        return None
    if preview.workspace_source is None:
        return (
            "Workspace-enabled real execution requires a trusted "
            "blob_exact worktree source snapshot."
        )
    return None


def fulfill_duplicate_skip_branch_exports(
    context: WorkflowRunContext,
    preview: PreflightCompilationPreview,
    successful_run: RunHistoryRecord,
) -> None:
    try:
        plan = PreflightExecutionPlan.from_preview(
            preview=preview,
            run_id=successful_run.manifest.run_id,
            run_key_name=successful_run.manifest.run_key_name,
            project_root=context.project_root.as_posix(),
            context_root=context.project_root.as_posix(),
            manifest_root=(successful_run.run_dir / "manifests").as_posix(),
            created_at=datetime.now(),
        )
        branch_export_records = fulfill_branch_exports_from_history(
            plan, successful_run
        )
        refresh_historical_run_summary(plan, successful_run)
    except Exception as exc:
        context.console.print(f"[red]Branch export failed:[/] {exc}")
        raise typer.Exit(code=1) from exc
    print_branch_export_fulfillments(branch_export_records, context.console)


def write_initial_run_manifest(
    output: ArtifactStorePort,
    plan: PreflightExecutionPlan,
    source: PreflightWorkflowSource,
    resume_plan: ResumePlan,
) -> None:
    resume_source = resume_plan.decision.resume_source
    manifest = build_run_manifest_from_plan(
        plan=plan,
        source=source,
        workflow_identity=resume_plan.workflow_identity,
        resumed_nodes=resume_plan.resumed_node_ids,
        resume_source_run_id=(
            resume_source.manifest.run_id if resume_source is not None else None
        ),
        resume_source_run_key_name=(
            resume_source.manifest.run_key_name if resume_source is not None else None
        ),
    )
    write_running_run_manifest(output, manifest)
