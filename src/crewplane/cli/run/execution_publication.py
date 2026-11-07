"""Own blocking run publication until its writes and cleanup have settled."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextvars import copy_context
from functools import partial
from typing import TYPE_CHECKING, Never

import typer

from crewplane.architecture.ports import ArtifactStorePort
from crewplane.artifacts.manager import OutputManager
from crewplane.core.execution_state import TerminalRunStatus
from crewplane.core.preflight import PreflightExecutionPlan
from crewplane.core.preflight.diagnostics import (
    PreflightDiagnostic,
    PreflightDiagnosticCode,
    PreflightDiagnosticPhase,
)
from crewplane.observability import ObservabilityHub, PersistentRunLogger
from crewplane.runtime.agent.process.drain import unconfirmed_process_cleanup
from crewplane.runtime.agent.process.session import wait_for_owned_task

from .preflight import write_preflight_diagnostics
from .terminalization import (
    TerminalizationCoordinator,
    commit_terminalization_with_retry,
)
from .topology import workflow_topology_from_plan

if TYPE_CHECKING:
    from rich.console import Console

    from crewplane.artifacts.locks import SameContextLock

    from .completion import CompletionDisplay, RunArtifactLocations
    from .context import WorkflowRunContext
    from .observability import ObservabilityHubFactory, WorkflowWarningRecorder


async def settle_run_publication[**Parameters, Result](
    operation: Callable[Parameters, Result],
    *args: Parameters.args,
    **kwargs: Parameters.kwargs,
) -> tuple[Result, asyncio.CancelledError | None]:
    """Drain a publication and return cancellation for its owner to propagate.

    Owners that acquire resources or select a terminal outcome must finish their
    lifecycle transition before propagating cancellation. Retain a worker failure
    as its cause when both occur.
    """
    worker = asyncio.get_running_loop().run_in_executor(
        None, copy_context().run, partial(operation, *args, **kwargs)
    )
    cancellation = await wait_for_owned_task(worker)
    try:
        result = worker.result()
    except BaseException as exc:
        if cancellation is not None:
            raise cancellation from exc
        raise
    return result, cancellation


async def run_owned_publication[**Parameters, Result](
    operation: Callable[Parameters, Result],
    *args: Parameters.args,
    **kwargs: Parameters.kwargs,
) -> Result:
    """Drain run-owned I/O before propagating cancellation or worker failure."""
    result, cancellation = await settle_run_publication(operation, *args, **kwargs)
    if cancellation is not None:
        raise cancellation
    return result


async def terminalize_cancelled_run_setup(
    output: ArtifactStorePort,
    plan: PreflightExecutionPlan,
    warning_recorder: WorkflowWarningRecorder,
    terminalization: TerminalizationCoordinator,
    reason: str,
    observability_hub_cls: ObservabilityHubFactory | None,
) -> None:
    """Drain cancelled setup publication before its owner releases the run."""
    await run_owned_publication(
        terminalize_run_setup,
        output,
        plan,
        warning_recorder,
        terminalization,
        "cancelled",
        reason,
        observability_hub_cls,
    )


def release_and_display_run(
    lock: SameContextLock,
    terminal_complete: bool,
    active_error: BaseException | None,
    artifact_locations: RunArtifactLocations | None,
    display: CompletionDisplay,
    console: Console,
) -> tuple[bool, bool]:
    """Release run ownership before publishing the retained completion display."""
    cleanup_confirmed = unconfirmed_process_cleanup(active_error) is None
    if artifact_locations is not None:
        artifact_locations.lock_released = False
        artifact_locations.cleanup_confirmed = cleanup_confirmed
    released = lock.release(
        terminal_complete=terminal_complete,
        cleanup_unconfirmed=not cleanup_confirmed,
    )
    if artifact_locations is not None:
        artifact_locations.lock_released = released
    return released, display.display(console)


def raise_runtime_config_preflight_failure(
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
    write_preflight_diagnostics(fallback_output, [diagnostic], context.workflow.name)
    context.console.print(f"[red]Preflight RUNTIME-CONFIG:[/] {exc}")
    raise typer.Exit(code=1) from exc


def terminalize_run_setup(
    output: ArtifactStorePort,
    plan: PreflightExecutionPlan,
    warning_recorder: WorkflowWarningRecorder,
    terminalization: TerminalizationCoordinator,
    status: TerminalRunStatus,
    reason: str,
    observability_hub_cls: ObservabilityHubFactory | None,
) -> None:
    """Publish a setup outcome, stop its observers, then commit the manifest."""
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
        commit_terminalization_with_retry(terminalization, hub, status, reason)
    terminalization.acknowledge_observer_shutdown()
