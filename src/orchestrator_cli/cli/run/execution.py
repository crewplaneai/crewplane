from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import typer
from rich.console import Console

from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.architecture.ports.runtime import RuntimeComponents
from orchestrator_cli.artifacts.locks import acquire_same_context_lock
from orchestrator_cli.artifacts.manager import OutputManager
from orchestrator_cli.artifacts.resume_hydration import hydrate_resume_frontier
from orchestrator_cli.bootstrap import build_runtime_config_snapshot
from orchestrator_cli.core.config import Config
from orchestrator_cli.core.preflight import PreflightExecutionPlan
from orchestrator_cli.core.preflight.diagnostics import PreflightDiagnostic
from orchestrator_cli.core.preflight.secrets import SecretContext
from orchestrator_cli.core.preflight.source import PreflightWorkflowSource
from orchestrator_cli.observability import PersistentRunLogger
from orchestrator_cli.runtime.execution import execute_workflow

from .components import allocate_run_output, build_components_for_run
from .context import WorkflowRunContext, resolve_orchestrator_dir, resolve_project_root
from .manifest import (
    build_run_manifest_from_plan,
    finalize_run_manifest,
    print_duplicate_context_message,
    print_resume_context_message,
    write_running_run_manifest,
)
from .observability import (
    EXTERNAL_CANCEL_REASON,
    UI_STOP_CANCEL_REASON,
    ExecuteWorkflowCallable,
    ObservabilityHubFactory,
    WorkflowCancelledByUser,
    WorkflowWarningRecorder,
    execute_workflow_with_observability,
    print_end_of_run_summary,
    refresh_failed_run_summary,
)
from .preflight import (
    compile_preview,
    materialize_preflight_success,
    print_preflight_diagnostics,
    run_cli_availability_errors,
    write_preflight_diagnostics,
    write_preflight_failure_artifacts,
)
from .resume import (
    ResumePlan,
    build_resume_plan,
    require_filesystem_artifacts_backend,
    workflow_identity_for_source,
)
from .topology import workflow_topology_from_plan, workflow_topology_from_preview


async def run_and_finalize_workflow(
    context: WorkflowRunContext,
    output: ArtifactStorePort,
    components: RuntimeComponents,
    plan: PreflightExecutionPlan,
    secret_context: SecretContext,
    execute_workflow_impl: ExecuteWorkflowCallable,
    warning_recorder: WorkflowWarningRecorder,
    observability_hub_cls: ObservabilityHubFactory | None,
    workflow_identity: str,
    resumed_node_ids: tuple[str, ...] = (),
) -> None:
    try:
        persistent_logger = PersistentRunLogger(output)
        warning_recorder.bind_logger(persistent_logger)
        await execute_workflow_with_observability(
            components,
            workflow_topology_from_plan(plan),
            plan,
            secret_context,
            output,
            execute_workflow_impl,
            persistent_logger,
            warning_recorder,
            observability_hub_cls,
            workflow_identity=workflow_identity,
            resumed_node_ids=resumed_node_ids,
        )
    except asyncio.CancelledError:
        finalize_run_manifest(
            output,
            "cancelled",
            cancel_reason=EXTERNAL_CANCEL_REASON,
        )
        print_end_of_run_summary(context.console, warning_recorder.persistent_logger)
        raise
    except WorkflowCancelledByUser:
        finalize_run_manifest(
            output,
            "cancelled",
            cancel_reason=UI_STOP_CANCEL_REASON,
        )
        print_end_of_run_summary(context.console, warning_recorder.persistent_logger)
        raise
    except Exception as exc:
        finalize_run_manifest(
            output,
            "failed",
            failure_message=str(exc),
        )
        summary_logger = refresh_failed_run_summary(
            warning_recorder.persistent_logger,
            context.workflow,
            warning_recorder.run_id,
            exc,
        )
        print_end_of_run_summary(context.console, summary_logger)
        raise

    finalize_run_manifest(
        output,
        "succeeded",
    )
    print_end_of_run_summary(context.console, warning_recorder.persistent_logger)


async def execute_workflow_run(
    config: Config,
    source: PreflightWorkflowSource,
    force: bool,
    no_live: bool,
    console: Console,
    execute_workflow_impl: ExecuteWorkflowCallable = execute_workflow,
    observability_hub_cls: ObservabilityHubFactory | None = None,
    which_fn: Callable[[str], str | None] | None = None,
    project_root: Path | None = None,
    orchestrator_dir: Path | None = None,
) -> None:
    """Compile preflight, execute the plan, and finalize the run manifest."""

    resolved_project_root = resolve_project_root(project_root)
    context = WorkflowRunContext(
        config=config,
        source=source,
        console=console,
        project_root=resolved_project_root,
        orchestrator_dir=resolve_orchestrator_dir(
            resolved_project_root,
            orchestrator_dir,
        ),
    )
    workflow = source.workflow
    warning_recorder = WorkflowWarningRecorder(workflow=workflow, console=console)
    try:
        snapshot_result = build_runtime_config_snapshot(
            config=config,
            console=console,
            no_live=no_live,
        )
    except Exception as exc:
        diagnostics = [
            PreflightDiagnostic(
                code="RUNTIME-CONFIG",
                phase="validation",
                message=str(exc),
            )
        ]
        fallback_output = OutputManager(
            workflow.name,
            base_dir=context.orchestrator_dir,
            template_base_dir=context.project_root,
            log_cli_output=False,
        )
        write_preflight_diagnostics(fallback_output, diagnostics, workflow.name)
        context.console.print(f"[red]Preflight RUNTIME-CONFIG:[/] {exc}")
        raise typer.Exit(code=1) from exc
    preview = compile_preview(
        context=context,
        snapshot_result=snapshot_result,
        fingerprint_key_policy="persist_if_needed",
        additional_validation_errors=run_cli_availability_errors(
            workflow,
            config,
            which_fn,
        ),
    )
    print_preflight_diagnostics(preview.diagnostics, context.console)
    if preview.has_errors() or preview.workflow_signature is None:
        write_preflight_failure_artifacts(
            context=context,
            snapshot_result=snapshot_result,
            diagnostics=preview.diagnostics,
            workflow_name=workflow.name,
        )
        for diagnostic in preview.diagnostics:
            context.console.print(
                f"[red]Preflight {diagnostic.code}:[/] {diagnostic.message}"
            )
        raise typer.Exit(code=1)

    require_filesystem_artifacts_backend(config)
    workflow_identity = workflow_identity_for_source(source, context.project_root)
    same_context_lock = acquire_same_context_lock(
        context.orchestrator_dir,
        context.workflow.name,
        workflow_identity,
        preview.workflow_signature,
    )
    try:
        resume_plan = build_resume_plan(
            config,
            source,
            preview,
            context.project_root,
            context.orchestrator_dir,
            force,
        )
        if resume_plan.decision.kind == "skip":
            successful_run = resume_plan.decision.successful_run
            print_duplicate_context_message(
                context,
                (
                    successful_run.manifest.run_id
                    if successful_run is not None
                    else None
                ),
            )
            return

        workflow_topology = workflow_topology_from_preview(preview)
        output = allocate_run_output(context, snapshot_result, warning_recorder)
        same_context_lock.update_run(output.run_id, output.run_key_name)
        plan = materialize_preflight_success(output, preview)
        _write_initial_run_manifest(
            output,
            plan,
            source,
            resume_plan,
        )
        try:
            if resume_plan.frontier is not None:
                hydrate_resume_frontier(resume_plan.frontier, plan, output)
                source_run = resume_plan.decision.resume_source
                if source_run is not None:
                    print_resume_context_message(
                        context,
                        len(resume_plan.resumed_node_ids),
                        source_run.manifest.run_id,
                    )
            components = build_components_for_run(
                context=context,
                snapshot_result=snapshot_result,
                workflow_topology=workflow_topology,
                artifact_store=output,
                no_live=no_live,
                warning_recorder=warning_recorder,
                which_fn=which_fn,
            )
        except Exception as exc:
            finalize_run_manifest(output, "failed", failure_message=str(exc))
            raise

        await run_and_finalize_workflow(
            context,
            output,
            components,
            plan,
            preview.secret_context,
            execute_workflow_impl,
            warning_recorder,
            observability_hub_cls,
            workflow_identity=resume_plan.workflow_identity,
            resumed_node_ids=resume_plan.resumed_node_ids,
        )
    finally:
        same_context_lock.release()


def _write_initial_run_manifest(
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
