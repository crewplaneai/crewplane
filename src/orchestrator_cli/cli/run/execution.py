from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import typer
from rich.console import Console

from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.architecture.ports.runtime import RuntimeComponents
from orchestrator_cli.artifacts.manager import OutputManager
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
    PreparedExecutionManifest,
    build_manifest_from_plan,
    finalize_manifest,
    print_duplicate_context_message,
    workflow_signature_already_exists,
)
from .observability import (
    ExecuteWorkflowCallable,
    ObservabilityHubFactory,
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
from .topology import workflow_topology_from_plan, workflow_topology_from_preview


async def run_and_finalize_workflow(
    context: WorkflowRunContext,
    output: ArtifactStorePort,
    components: RuntimeComponents,
    plan: PreflightExecutionPlan,
    secret_context: SecretContext,
    prepared_manifest: PreparedExecutionManifest,
    execute_workflow_impl: ExecuteWorkflowCallable,
    warning_recorder: WorkflowWarningRecorder,
    observability_hub_cls: ObservabilityHubFactory | None,
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
        )
    except Exception as exc:
        finalize_manifest(
            output,
            prepared_manifest.workflow_signature,
            prepared_manifest.manifest,
            "failed",
        )
        summary_logger = refresh_failed_run_summary(
            warning_recorder.persistent_logger,
            context.workflow,
            warning_recorder.run_id,
            exc,
        )
        print_end_of_run_summary(context.console, summary_logger)
        raise

    finalize_manifest(
        output,
        prepared_manifest.workflow_signature,
        prepared_manifest.manifest,
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
            workflow_schema_version=workflow.schema_version,
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

    if workflow_signature_already_exists(
        context,
        snapshot_result,
        preview.workflow_signature,
        force,
    ):
        print_duplicate_context_message(context)
        return

    workflow_topology = workflow_topology_from_preview(preview)
    output = allocate_run_output(context, snapshot_result, warning_recorder)
    plan = materialize_preflight_success(output, preview)
    components = build_components_for_run(
        context=context,
        snapshot_result=snapshot_result,
        workflow_topology=workflow_topology,
        artifact_store=output,
        no_live=no_live,
        warning_recorder=warning_recorder,
        which_fn=which_fn,
    )
    prepared_manifest = PreparedExecutionManifest(
        workflow_signature=plan.workflow_signature,
        manifest=build_manifest_from_plan(plan=plan, source=source),
    )

    await run_and_finalize_workflow(
        context,
        output,
        components,
        plan,
        preview.secret_context,
        prepared_manifest,
        execute_workflow_impl,
        warning_recorder,
        observability_hub_cls,
    )
