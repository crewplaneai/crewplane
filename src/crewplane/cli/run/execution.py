from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Never

import typer
from rich.console import Console

from crewplane.architecture.errors import AdapterContractError
from crewplane.architecture.ports import ArtifactStorePort
from crewplane.architecture.ports.runtime import RuntimeComponents
from crewplane.artifacts.locks import acquire_same_context_lock
from crewplane.artifacts.manager import OutputManager
from crewplane.artifacts.resume.checkpoint_hydration import hydrate_review_checkpoints
from crewplane.artifacts.resume.expansions import hydrate_expansions
from crewplane.artifacts.resume.hydration import hydrate_resume_frontier
from crewplane.bootstrap import (
    build_runtime_config_snapshot,
)
from crewplane.core.config import Config
from crewplane.core.preflight import (
    PreflightExecutionPlan,
)
from crewplane.core.preflight.diagnostics import (
    PreflightDiagnostic,
    PreflightDiagnosticCode,
    PreflightDiagnosticPhase,
)
from crewplane.core.preflight.foreach.compilation import inventory_workflow
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.preflight.source import PreflightWorkflowSource
from crewplane.observability import ObservabilityHub, PersistentRunLogger
from crewplane.runtime.agent.process.drain import unconfirmed_process_cleanup
from crewplane.runtime.execution import execute_workflow
from crewplane.runtime.workspace.branch_export import (
    fulfill_branch_exports,
)

from .branch_export_output import print_branch_export_fulfillments
from .completion import (
    CompletedRun,
    CompletionDisplay,
    RunArtifactLocations,
    persist_memory_completion,
)
from .components import allocate_run_output, build_components_for_run
from .context import WorkflowRunContext, build_workflow_run_context
from .execution_helpers import (
    fulfill_historical_branch_exports,
    handle_duplicate_skip,
    raise_for_workspace_runtime_error,
    raise_run_preflight_errors,
    write_initial_run_manifest,
)
from .invoker_preflight import collect_invoker_availability_diagnostics
from .manifest import (
    print_resume_context_message,
)
from .observability import (
    ExecuteWorkflowCallable,
    ObservabilityHubFactory,
    WorkflowCancelledByUser,
    WorkflowWarningRecorder,
    execute_workflow_with_observability,
    print_end_of_run_summary,
)
from .preflight import (
    compile_preview_async,
    materialize_preflight_success,
    print_preflight_diagnostics,
    uses_mock_invoker,
    write_preflight_diagnostics,
)
from .resume import (
    build_resume_plan,
    require_filesystem_artifacts_backend,
    workflow_identity_for_source,
)
from .terminalization import (
    TerminalizationCoordinator,
    commit_terminalization_with_retry,
)
from .topology import workflow_topology_from_plan, workflow_topology_from_preview


def _raise_runtime_config_preflight_failure(
    context: WorkflowRunContext,
    exc: Exception,
) -> Never:
    diagnostic = PreflightDiagnostic(
        code=PreflightDiagnosticCode.RUNTIME_CONFIG,
        phase=PreflightDiagnosticPhase.VALIDATION,
        message=str(exc),
    )
    fallback_output = OutputManager(
        context.workflow.name,
        base_dir=context.state_dir,
        template_base_dir=context.project_root,
        log_cli_output=False,
    )
    write_preflight_diagnostics(
        fallback_output,
        [diagnostic],
        context.workflow.name,
    )
    context.console.print(f"[red]Preflight RUNTIME-CONFIG:[/] {exc}")
    raise typer.Exit(code=1) from exc


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
    terminalization: TerminalizationCoordinator,
    resumed_node_ids: tuple[str, ...] = (),
    deferred_display: CompletionDisplay | None = None,
) -> None:
    branch_export_records: tuple[Path, ...] = ()
    persistent_logger: PersistentRunLogger | None = None

    def complete_scheduler_postconditions(
        effective_plan: PreflightExecutionPlan,
    ) -> None:
        nonlocal branch_export_records
        branch_export_records = fulfill_branch_exports(
            effective_plan,
            output,
            resumed_node_ids=resumed_node_ids,
        )

    try:
        persistent_logger = PersistentRunLogger(output)
        if deferred_display is not None:
            deferred_display.logger = persistent_logger
        terminalization.bind_summary_logger(persistent_logger)
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
            on_scheduler_succeeded=complete_scheduler_postconditions,
            terminalization=terminalization,
            workflow_identity=workflow_identity,
            resumed_node_ids=resumed_node_ids,
        )
    except asyncio.CancelledError:
        if deferred_display is None:
            print_end_of_run_summary(context.console, persistent_logger)
        raise
    except WorkflowCancelledByUser:
        if deferred_display is None:
            print_end_of_run_summary(context.console, persistent_logger)
        raise
    except Exception:
        if deferred_display is None:
            print_end_of_run_summary(context.console, persistent_logger)
        raise
    if deferred_display is not None:
        deferred_display.branch_exports = branch_export_records
    else:
        print_branch_export_fulfillments(branch_export_records, context.console)
        print_end_of_run_summary(context.console, persistent_logger)


def terminalize_component_setup_failure(
    output: ArtifactStorePort,
    plan: PreflightExecutionPlan,
    warning_recorder: WorkflowWarningRecorder,
    terminalization: TerminalizationCoordinator,
    exc: Exception,
    observability_hub_cls: ObservabilityHubFactory | None,
) -> None:
    persistent_logger = PersistentRunLogger(output)
    terminalization.bind_summary_logger(persistent_logger)
    warning_recorder.bind_logger(persistent_logger)
    observability_hub_factory = (
        ObservabilityHub if observability_hub_cls is None else observability_hub_cls
    )
    with observability_hub_factory(
        workflow_topology=workflow_topology_from_plan(plan),
        run_id=output.run_id,
        observers=[persistent_logger],
        refresh_per_second=0,
        warning_sink=warning_recorder.sink,
    ) as hub:
        warning_recorder.flush_queued()
        commit_terminalization_with_retry(terminalization, hub, "failed", str(exc))
    terminalization.acknowledge_observer_shutdown()


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
    state_dir: Path | None = None,
    runtime_variables: dict[str, str] | None = None,
    defer_terminal_failure: bool = False,
    artifact_locations: RunArtifactLocations | None = None,
) -> CompletedRun:
    """Compile preflight, execute the plan, and finalize the run manifest."""

    context = build_workflow_run_context(
        config, source, console, project_root, state_dir
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
        _raise_runtime_config_preflight_failure(context, exc)
    if uses_mock_invoker(config):
        context.console.print(
            "Mock invoker active: no provider CLI commands will be started."
        )
    try:
        invoker_diagnostics = await collect_invoker_availability_diagnostics(
            inventory_workflow(source),
            config,
            context.project_root,
            snapshot_result.invoker_options,
            which_fn,
        )
        preview = await compile_preview_async(
            context=context,
            snapshot_result=snapshot_result,
            fingerprint_key_policy="persist_if_needed",
            invoker_diagnostics=invoker_diagnostics,
            workspace_real_execution=True,
            runtime_variables=runtime_variables,
        )
    except AdapterContractError as exc:
        _raise_runtime_config_preflight_failure(context, exc)
    print_preflight_diagnostics(preview.diagnostics, context.console)
    if preview.has_errors() or preview.workflow_signature is None:
        raise_run_preflight_errors(context, snapshot_result, preview, workflow.name)

    raise_for_workspace_runtime_error(context, snapshot_result, config, preview)

    require_filesystem_artifacts_backend(config)
    workflow_identity = workflow_identity_for_source(source, context.project_root)
    same_context_lock = acquire_same_context_lock(
        context.state_dir,
        context.workflow.name,
        workflow_identity,
        preview.workflow_signature,
    )
    terminalization: TerminalizationCoordinator | None = None
    running_manifest_written = False
    display = CompletionDisplay()
    terminal_error: Exception | None = None
    completed_plan: PreflightExecutionPlan | None = None
    completed_output: ArtifactStorePort | None = None
    cancelled = False
    try:
        resume_plan = build_resume_plan(
            config,
            source,
            preview,
            context.project_root,
            context.state_dir,
            force,
        )
        if handle_duplicate_skip(context, preview, resume_plan):
            return CompletedRun("dedupe", context.project_root, context.state_dir)
        resume_source = resume_plan.decision.resume_source
        if resume_source is not None:
            fulfill_historical_branch_exports(
                context,
                preview,
                resume_source,
                resume_plan.resumed_node_ids,
            )

        workflow_topology = workflow_topology_from_preview(preview)
        output = allocate_run_output(context, snapshot_result, warning_recorder)
        same_context_lock.update_run(output.run_id, output.run_key_name)
        plan = materialize_preflight_success(output, preview, context.project_root)
        completed_plan, completed_output = plan, output
        if artifact_locations is not None:
            artifact_locations.plan = plan
            artifact_locations.stages_dir = output.stages_dir
            artifact_locations.results_dir = output.results_dir
        write_initial_run_manifest(
            output,
            plan,
            source,
            resume_plan,
        )
        running_manifest_written = True
        terminalization = TerminalizationCoordinator(
            output=output,
            workflow_name=plan.workflow_name,
            terminal_recovery_recorder=same_context_lock,
        )
        try:
            if resume_plan.frontier is not None:
                plan = hydrate_expansions(resume_plan.frontier, plan, output)
                workflow_topology = workflow_topology_from_plan(plan)
                hydrate_resume_frontier(resume_plan.frontier, plan, output)
                hydrate_review_checkpoints(resume_plan.frontier, plan, output)
                if resume_source is not None:
                    print_resume_context_message(
                        context,
                        len(resume_plan.resumed_node_ids),
                        resume_source.manifest.run_id,
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
            terminalize_component_setup_failure(
                output,
                plan,
                warning_recorder,
                terminalization,
                exc,
                observability_hub_cls,
            )
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
            terminalization=terminalization,
            resumed_node_ids=resume_plan.resumed_node_ids,
            deferred_display=display,
        )
    except (asyncio.CancelledError, WorkflowCancelledByUser, KeyboardInterrupt):
        cancelled = True
        raise
    except Exception as exc:
        terminal_error = exc
    finally:
        terminal_complete = not running_manifest_written or (
            terminalization is not None and terminalization.committed
        )
        cleanup_confirmed = (
            unconfirmed_process_cleanup(terminal_error or sys.exception()) is None
        )
        if artifact_locations is not None:
            artifact_locations.lock_released = False
            artifact_locations.cleanup_confirmed = cleanup_confirmed
        released = same_context_lock.release(
            terminal_complete=terminal_complete,
            cleanup_unconfirmed=not cleanup_confirmed,
        )
        if artifact_locations is not None:
            artifact_locations.lock_released = released

        displayed = display.display(context.console)
    cleanup_confirmed = unconfirmed_process_cleanup(terminal_error) is None
    completion = CompletedRun(
        kind="executed"
        if completed_plan is not None and display.logger is not None
        else "ineligible",
        project_root=context.project_root,
        state_dir=context.state_dir,
        plan=completed_plan,
        stages_dir=completed_output.stages_dir
        if completed_output is not None
        else None,
        results_dir=completed_output.results_dir
        if completed_output is not None
        else None,
        outcome="failed" if terminal_error else "succeeded",
        terminal_error=terminal_error,
        manifest_committed=terminalization is not None and terminalization.committed,
        cleanup_confirmed=cleanup_confirmed and not cancelled,
        lock_released=released,
        result_displayed=displayed,
    )
    if any(node.memory for node in inventory_workflow(source).nodes):
        completion = persist_memory_completion(completion, context.console)
    if terminal_error is not None and (
        not defer_terminal_failure or not completion.can_learn
    ):
        raise terminal_error
    return completion
