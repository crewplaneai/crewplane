"""Coordinate preflight, owned run execution, terminalization, and completion."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path

from rich.console import Console

from crewplane.architecture.errors import AdapterContractError
from crewplane.architecture.ports import ArtifactStorePort
from crewplane.architecture.ports.runtime import RuntimeComponents
from crewplane.artifacts.locks import acquire_same_context_lock
from crewplane.artifacts.resume.expansions import hydrate_expansions
from crewplane.bootstrap import (
    RuntimeConfigSnapshotBuildResult,
    build_runtime_config_snapshot,
)
from crewplane.core.config import Config
from crewplane.core.preflight import (
    PreflightCompilationPreview,
    PreflightExecutionPlan,
)
from crewplane.core.preflight.foreach.compilation import inventory_workflow
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.preflight.source import PreflightWorkflowSource
from crewplane.observability import PersistentRunLogger
from crewplane.observability.types import WorkflowTopology
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
    raise_for_workspace_runtime_error,
    raise_run_preflight_errors,
    write_initial_run_manifest,
)
from .execution_publication import (
    raise_runtime_config_preflight_failure,
    release_and_display_run,
    run_owned_publication,
    settle_run_publication,
    terminalize_cancelled_run_setup,
    terminalize_run_setup,
)
from .execution_resume import hydrate_run_resume, prepare_run_resume
from .invoker_preflight import collect_invoker_availability_diagnostics
from .observability import (
    EXTERNAL_CANCEL_REASON,
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
)
from .resume import (
    ResumePlan,
    require_filesystem_artifacts_backend,
    workflow_identity_for_source,
)
from .terminalization import TerminalizationCoordinator
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
    terminalization: TerminalizationCoordinator,
    resumed_node_ids: tuple[str, ...] = (),
    deferred_display: CompletionDisplay | None = None,
) -> None:
    """Execute and terminalize, propagating execution and publication failures.

    Branch exports precede successful terminalization; observer shutdown precedes
    the terminal manifest commit. A deferred display receives the logger and
    successful export records so its caller can print after releasing the lock.
    Otherwise, print the summary here, including on failure or cancellation.
    """
    branch_export_records: tuple[Path, ...] = ()
    persistent_logger: PersistentRunLogger | None = None

    def record_summary_logger(logger: PersistentRunLogger | None) -> None:
        nonlocal persistent_logger
        persistent_logger = logger
        if deferred_display is not None:
            deferred_display.logger = logger

    async def complete_scheduler_postconditions(
        effective_plan: PreflightExecutionPlan,
    ) -> None:
        nonlocal branch_export_records
        branch_export_records = await run_owned_publication(
            fulfill_branch_exports,
            effective_plan,
            output,
            resumed_node_ids=resumed_node_ids,
        )

    try:
        logger, cancellation = await settle_run_publication(PersistentRunLogger, output)
        record_summary_logger(logger)
        terminalization.bind_summary_logger(logger)
        warning_recorder.bind_logger(logger)
        if cancellation is not None:
            try:
                await terminalize_cancelled_run_setup(
                    output,
                    plan,
                    warning_recorder,
                    terminalization,
                    EXTERNAL_CANCEL_REASON,
                    observability_hub_cls,
                )
            finally:
                record_summary_logger(terminalization.summary_logger)
            raise cancellation
        await execute_workflow_with_observability(
            components,
            workflow_topology_from_plan(plan),
            plan,
            secret_context,
            output,
            execute_workflow_impl,
            logger,
            warning_recorder,
            observability_hub_cls,
            on_scheduler_succeeded=complete_scheduler_postconditions,
            terminalization=terminalization,
            workflow_identity=workflow_identity,
            resumed_node_ids=resumed_node_ids,
        )
    except (asyncio.CancelledError, Exception):
        if deferred_display is None:
            print_end_of_run_summary(context.console, persistent_logger)
        raise
    if deferred_display is not None:
        deferred_display.branch_exports = branch_export_records
    else:

        def print_completion() -> None:
            print_branch_export_fulfillments(branch_export_records, context.console)
            print_end_of_run_summary(context.console, persistent_logger)

        await run_owned_publication(print_completion)


def terminalize_component_setup_failure(
    output: ArtifactStorePort,
    plan: PreflightExecutionPlan,
    warning_recorder: WorkflowWarningRecorder,
    terminalization: TerminalizationCoordinator,
    exc: Exception,
    observability_hub_cls: ObservabilityHubFactory | None,
) -> None:
    """Publish failed terminal views and commit after observer shutdown.

    Bind the summary logger and flush queued warnings. Publication and observer
    errors propagate; the caller retains responsibility for the setup exception,
    completion display, and lock release.
    """
    terminalize_run_setup(
        output,
        plan,
        warning_recorder,
        terminalization,
        "failed",
        str(exc),
        observability_hub_cls,
    )


async def _prepare_run_components(
    context: WorkflowRunContext,
    snapshot_result: RuntimeConfigSnapshotBuildResult,
    workflow_topology: WorkflowTopology,
    output: ArtifactStorePort,
    plan: PreflightExecutionPlan,
    resume_plan: ResumePlan,
    no_live: bool,
    warning_recorder: WorkflowWarningRecorder,
    which_fn: Callable[[str], str | None] | None,
    terminalization: TerminalizationCoordinator,
    observability_hub_cls: ObservabilityHubFactory | None,
    display: CompletionDisplay,
    cancellation: asyncio.CancelledError | None,
) -> tuple[PreflightExecutionPlan, RuntimeComponents]:
    """Restore resume artifacts and build components within setup recovery."""
    try:
        if cancellation is not None:
            raise cancellation
        if resume_plan.frontier is not None:
            plan, cancellation = await settle_run_publication(
                hydrate_expansions, resume_plan.frontier, plan, output
            )
            workflow_topology = workflow_topology_from_plan(plan)
            if cancellation is not None:
                raise cancellation
            await run_owned_publication(
                hydrate_run_resume, context, resume_plan, plan, output
            )
        components = await run_owned_publication(
            build_components_for_run,
            context=context,
            snapshot_result=snapshot_result,
            workflow_topology=workflow_topology,
            artifact_store=output,
            no_live=no_live,
            warning_recorder=warning_recorder,
            which_fn=which_fn,
        )
    except asyncio.CancelledError:
        try:
            await terminalize_cancelled_run_setup(
                output,
                plan,
                warning_recorder,
                terminalization,
                EXTERNAL_CANCEL_REASON,
                observability_hub_cls,
            )
        finally:
            display.logger = terminalization.summary_logger
        raise
    except Exception as exc:
        await run_owned_publication(
            terminalize_component_setup_failure,
            output,
            plan,
            warning_recorder,
            terminalization,
            exc,
            observability_hub_cls,
        )
        raise
    return plan, components


async def _compile_run_preflight(
    context: WorkflowRunContext,
    no_live: bool,
    which_fn: Callable[[str], str | None] | None,
    runtime_variables: dict[str, str] | None,
) -> tuple[RuntimeConfigSnapshotBuildResult, PreflightCompilationPreview]:
    config, source = context.config, context.source
    try:
        snapshot_result = build_runtime_config_snapshot(
            config=config, console=context.console, no_live=no_live
        )
    except Exception as exc:
        return await run_owned_publication(
            raise_runtime_config_preflight_failure, context, exc
        )
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
        return await run_owned_publication(
            raise_runtime_config_preflight_failure, context, exc
        )
    print_preflight_diagnostics(preview.diagnostics, context.console)
    if preview.has_errors() or preview.workflow_signature is None:
        await run_owned_publication(
            raise_run_preflight_errors,
            context,
            snapshot_result,
            preview,
            context.workflow.name,
        )
    await run_owned_publication(
        raise_for_workspace_runtime_error, context, snapshot_result, config, preview
    )
    require_filesystem_artifacts_backend(config)
    return snapshot_result, preview


def _publish_run_completion(
    context: WorkflowRunContext,
    plan: PreflightExecutionPlan | None,
    output: ArtifactStorePort | None,
    terminalization: TerminalizationCoordinator | None,
    terminal_error: Exception | None,
    display: CompletionDisplay,
    released: bool,
    displayed: bool,
    defer_terminal_failure: bool,
) -> CompletedRun:
    completion = CompletedRun(
        kind="executed"
        if plan is not None and display.logger is not None
        else "ineligible",
        project_root=context.project_root,
        state_dir=context.state_dir,
        plan=plan,
        stages_dir=output.stages_dir if output is not None else None,
        results_dir=output.results_dir if output is not None else None,
        outcome="failed" if terminal_error else "succeeded",
        terminal_error=terminal_error,
        manifest_committed=terminalization is not None and terminalization.committed,
        cleanup_confirmed=unconfirmed_process_cleanup(terminal_error) is None,
        lock_released=released,
        result_displayed=displayed,
    )
    if any(node.memory for node in inventory_workflow(context.source).nodes):
        completion = persist_memory_completion(completion, context.console)
    if terminal_error is not None and (
        not defer_terminal_failure or not completion.can_learn
    ):
        raise terminal_error
    return completion


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
    warning_recorder = WorkflowWarningRecorder(
        workflow=context.workflow, console=console
    )
    snapshot_result, preview = await _compile_run_preflight(
        context, no_live, which_fn, runtime_variables
    )
    assert preview.workflow_signature is not None
    workflow_identity = workflow_identity_for_source(source, context.project_root)
    same_context_lock, cancellation = await settle_run_publication(
        acquire_same_context_lock,
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
    try:
        if cancellation is not None:
            raise cancellation
        resume_plan = await run_owned_publication(
            prepare_run_resume, context, preview, force
        )
        if resume_plan is None:
            return CompletedRun("dedupe", context.project_root, context.state_dir)

        workflow_topology = workflow_topology_from_preview(preview)
        output = await run_owned_publication(
            allocate_run_output, context, snapshot_result, warning_recorder
        )
        await run_owned_publication(
            same_context_lock.update_run, output.run_id, output.run_key_name
        )
        plan = await run_owned_publication(
            materialize_preflight_success, output, preview, context.project_root
        )
        completed_plan, completed_output = plan, output
        if artifact_locations is not None:
            artifact_locations.plan = plan
            artifact_locations.stages_dir = output.stages_dir
            artifact_locations.results_dir = output.results_dir
        _, cancellation = await settle_run_publication(
            write_initial_run_manifest, output, plan, source, resume_plan
        )
        running_manifest_written = True
        terminalization = TerminalizationCoordinator(
            output=output,
            workflow_name=plan.workflow_name,
            terminal_recovery_recorder=same_context_lock,
        )
        plan, components = await _prepare_run_components(
            context,
            snapshot_result,
            workflow_topology,
            output,
            plan,
            resume_plan,
            no_live,
            warning_recorder,
            which_fn,
            terminalization,
            observability_hub_cls,
            display,
            cancellation,
        )

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
        raise
    except Exception as exc:
        terminal_error = exc
    finally:
        terminal_complete = not running_manifest_written or (
            terminalization is not None and terminalization.committed
        )
        active_error = terminal_error or sys.exception()
        (released, displayed), cancellation = await settle_run_publication(
            release_and_display_run,
            same_context_lock,
            terminal_complete,
            active_error,
            artifact_locations,
            display,
            context.console,
        )
        if cancellation is not None:
            raise cancellation from active_error
    return await run_owned_publication(
        _publish_run_completion,
        context,
        completed_plan,
        completed_output,
        terminalization,
        terminal_error,
        display,
        released,
        displayed,
        defer_terminal_failure,
    )
