from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from rich.console import Console

from crewplane.architecture.contracts import AgentInvoker
from crewplane.architecture.ports import ArtifactStorePort
from crewplane.architecture.ports.runtime import RuntimeComponents
from crewplane.core.preflight import PreflightExecutionPlan
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.workflow.models import WorkflowPlan
from crewplane.observability import (
    ObservabilityHub,
    PersistentRunLogger,
    RunResult,
)
from crewplane.observability.events import (
    EventSink,
    ExecutionEvent,
    runtime_log_event,
)
from crewplane.observability.observer import Observer
from crewplane.observability.persistent import render_run_summary_terminal
from crewplane.observability.types import WorkflowTopology

from .best_effort_thread import run_best_effort_thread

UI_STOP_POLL_INTERVAL_SECONDS = 0.1
EXTERNAL_CANCEL_REASON = "external_cancellation"
UI_STOP_CANCEL_REASON = "ui_stop_requested"
WORKFLOW_CANCELLED_MESSAGE = "Workflow cancelled by live dashboard quit request."
WARNING_RECORD_TIMEOUT_SECONDS = 0.1
SUMMARY_REFRESH_TIMEOUT_SECONDS = 1.0


class WorkflowCancelledByUser(RuntimeError):
    """Raised when a live UI requests workflow cancellation."""


class ExecuteWorkflowCallable(Protocol):
    async def __call__(
        self,
        plan: PreflightExecutionPlan,
        output: ArtifactStorePort,
        invoker: AgentInvoker,
        secret_context: SecretContext,
        event_sink: EventSink | None = None,
        run_id: str | None = None,
        suppress_progress_output: bool = False,
        workflow_identity: str | None = None,
        resumed_node_ids: tuple[str, ...] = (),
    ) -> None: ...


@dataclass
class WorkflowWarningRecorder:
    workflow: WorkflowPlan
    console: Console
    queued_warnings: list[str] = field(default_factory=list)
    run_id: str | None = None
    persistent_logger: PersistentRunLogger | None = None

    def bind_run_id(self, run_id: str) -> None:
        self.run_id = run_id

    def bind_logger(self, persistent_logger: PersistentRunLogger) -> None:
        self.persistent_logger = persistent_logger

    def sink(self, message: str) -> None:
        self.console.print(f"[yellow]WARN[/] {message}")
        if not self._can_write_to_logger():
            self.queued_warnings.append(message)
            return
        self._record_warning(message)

    def flush_queued(self) -> None:
        if not self._can_write_to_logger():
            return
        while self.queued_warnings:
            self._record_warning(self.queued_warnings.pop(0))

    def warning_event(self, message: str) -> ExecutionEvent:
        if self.run_id is None:
            raise RuntimeError("Run ID unavailable while recording workflow warning.")
        return runtime_log_event(
            workflow_name=self.workflow.name,
            run_id=self.run_id,
            level="warning",
            message=message,
            operation="runtime_warning",
        )

    def _can_write_to_logger(self) -> bool:
        return (
            self.persistent_logger is not None
            and self.run_id is not None
            and self.persistent_logger.started
        )

    def _record_warning(self, message: str) -> None:
        if self.persistent_logger is None:
            return

        def record_warning_event() -> None:
            if self.persistent_logger is not None:
                self.persistent_logger.record_event(self.warning_event(message))

        result = run_best_effort_thread(
            record_warning_event,
            name="crewplane-warning-persist",
            timeout_seconds=WARNING_RECORD_TIMEOUT_SECONDS,
        )
        if result.start_error is not None:
            self.console.print(
                "[yellow]WARN[/] persistent run logging unavailable while recording "
                f"workflow warning: {result.start_error}"
            )
            return
        if result.timed_out:
            self.console.print(
                "[yellow]WARN[/] persistent run logging timed out while recording "
                "workflow warning."
            )
            return
        if result.operation_error is not None:
            self.console.print(
                "[yellow]WARN[/] persistent run logging failed while recording "
                f"workflow warning: {result.operation_error}"
            )


class ObservabilityHubInstance(Protocol):
    active_observer_count: int
    stop_requested: bool

    def __enter__(self) -> ObservabilityHubInstance: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> bool | None: ...

    def emit(self, event: ExecutionEvent) -> None: ...


class ObservabilityHubFactory(Protocol):
    def __call__(
        self,
        workflow_topology: WorkflowTopology,
        run_id: str,
        observers: list[Observer],
        refresh_per_second: int = 4,
        warning_sink: Callable[[str], None] | None = None,
    ) -> ObservabilityHubInstance: ...


def observer_is_active(
    hub: ObservabilityHubInstance,
    observer: Observer,
    observer_count: int = 0,
) -> bool:
    checker = getattr(hub, "observer_is_active", None)
    if callable(checker):
        return bool(checker(observer))
    return observer_count > 0 and hub.active_observer_count > 1


async def wait_for_stop_request(hub: ObservabilityHubInstance) -> None:
    while not hub.stop_requested:
        await asyncio.sleep(UI_STOP_POLL_INTERVAL_SECONDS)


async def await_workflow_or_stop_request(
    workflow_call: Awaitable[None],
    hub: ObservabilityHubInstance,
) -> None:
    workflow_task = asyncio.create_task(workflow_call)
    stop_task = asyncio.create_task(wait_for_stop_request(hub))
    try:
        done, _ = await asyncio.wait(
            {workflow_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task in done and not workflow_task.done():
            workflow_task.cancel()
            try:
                await workflow_task
            except asyncio.CancelledError as exc:
                raise WorkflowCancelledByUser(WORKFLOW_CANCELLED_MESSAGE) from exc
            raise WorkflowCancelledByUser(WORKFLOW_CANCELLED_MESSAGE)
        await workflow_task
    except asyncio.CancelledError:
        workflow_task.cancel()
        await asyncio.gather(workflow_task, return_exceptions=True)
        raise
    finally:
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)


async def call_execute_workflow(
    execute_workflow_impl: ExecuteWorkflowCallable,
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    invoker: AgentInvoker,
    secret_context: SecretContext,
    event_sink: EventSink | None = None,
    run_id: str | None = None,
    suppress_progress_output: bool = False,
    workflow_identity: str | None = None,
    resumed_node_ids: tuple[str, ...] = (),
) -> None:
    await execute_workflow_impl(
        plan,
        output,
        invoker=invoker,
        secret_context=secret_context,
        event_sink=event_sink,
        run_id=run_id,
        suppress_progress_output=suppress_progress_output,
        workflow_identity=workflow_identity,
        resumed_node_ids=resumed_node_ids,
    )


def record_failure_summary_event(
    persistent_logger: PersistentRunLogger | None,
    workflow: WorkflowPlan,
    run_id: str | None,
    exc: Exception,
) -> None:
    if persistent_logger is None or run_id is None:
        return
    persistent_logger.record_failure_summary_event(
        workflow_name=workflow.name,
        run_id=run_id,
        message=str(exc),
    )


def refresh_failed_run_summary(
    persistent_logger: PersistentRunLogger | None,
    workflow: WorkflowPlan,
    run_id: str | None,
    exc: Exception,
) -> PersistentRunLogger | None:
    if persistent_logger is None:
        return None

    def refresh_summary() -> None:
        record_failure_summary_event(persistent_logger, workflow, run_id, exc)
        persistent_logger.refresh_summary(RunResult(status="failed"))

    result = run_best_effort_thread(
        refresh_summary,
        name="crewplane-failed-summary-refresh",
        timeout_seconds=SUMMARY_REFRESH_TIMEOUT_SECONDS,
    )
    if result.start_error is not None:
        exc.add_note(f"end-of-run summary refresh failed: {result.start_error}")
        return failed_summary_logger(persistent_logger)
    if result.timed_out:
        exc.add_note("end-of-run summary refresh timed out")
        return failed_summary_logger(persistent_logger)
    if result.operation_error is not None:
        exc.add_note(f"end-of-run summary refresh failed: {result.operation_error}")
    return failed_summary_logger(persistent_logger)


def refresh_successful_run_summary(
    persistent_logger: PersistentRunLogger | None,
) -> PersistentRunLogger | None:
    if persistent_logger is None:
        return None

    result = run_best_effort_thread(
        lambda: persistent_logger.refresh_summary(RunResult(status="succeeded")),
        name="crewplane-success-summary-refresh",
        timeout_seconds=SUMMARY_REFRESH_TIMEOUT_SECONDS,
    )
    if result.start_error is not None or result.timed_out:
        return persistent_logger
    if result.operation_error is not None:
        return persistent_logger
    return persistent_logger


def failed_summary_logger(
    persistent_logger: PersistentRunLogger,
) -> PersistentRunLogger | None:
    last_summary = persistent_logger.last_summary
    if last_summary is not None and last_summary.workflow_status == "succeeded":
        return None
    return persistent_logger


def print_end_of_run_summary(
    console: Console,
    persistent_logger: PersistentRunLogger | None,
) -> None:
    if persistent_logger is None:
        return
    summary = persistent_logger.last_summary
    if summary is None:
        return
    console.print("")
    console.print(render_run_summary_terminal(summary), markup=False)


def _set_cancelled_hub_terminal_result(
    hub: ObservabilityHubInstance,
    cancel_reason: str,
) -> None:
    setter = getattr(hub, "set_terminal_result", None)
    if callable(setter):
        setter(RunResult(status="cancelled", cancel_reason=cancel_reason))


async def execute_workflow_with_observability(
    components: RuntimeComponents,
    workflow_topology: WorkflowTopology,
    plan: PreflightExecutionPlan,
    secret_context: SecretContext,
    output: ArtifactStorePort,
    execute_workflow_impl: ExecuteWorkflowCallable,
    persistent_logger: PersistentRunLogger,
    warning_recorder: WorkflowWarningRecorder,
    observability_hub_cls: ObservabilityHubFactory | None,
    workflow_identity: str | None = None,
    resumed_node_ids: tuple[str, ...] = (),
) -> None:
    observability_hub_factory = (
        ObservabilityHub if observability_hub_cls is None else observability_hub_cls
    )
    with observability_hub_factory(
        workflow_topology=workflow_topology,
        run_id=output.run_id,
        observers=[persistent_logger, *components.observers],
        refresh_per_second=0,
        warning_sink=warning_recorder.sink,
    ) as hub:
        warning_recorder.flush_queued()
        ui_observers_unavailable = bool(components.observers) and not any(
            observer_is_active(
                hub,
                observer,
                observer_count=len(components.observers),
            )
            for observer in components.observers
        )
        if ui_observers_unavailable:
            warning_recorder.sink(
                "live dashboard unavailable; continuing without live dashboard."
            )
        selected_suppress_progress_output = (
            False if ui_observers_unavailable else components.suppress_progress_output
        )
        try:
            await await_workflow_or_stop_request(
                call_execute_workflow(
                    execute_workflow_impl,
                    plan,
                    output,
                    invoker=components.base_invoker,
                    secret_context=secret_context,
                    event_sink=hub.emit,
                    run_id=output.run_id,
                    suppress_progress_output=selected_suppress_progress_output,
                    workflow_identity=workflow_identity,
                    resumed_node_ids=resumed_node_ids,
                ),
                hub,
            )
        except asyncio.CancelledError:
            _set_cancelled_hub_terminal_result(hub, EXTERNAL_CANCEL_REASON)
            raise
        except WorkflowCancelledByUser:
            _set_cancelled_hub_terminal_result(hub, UI_STOP_CANCEL_REASON)
            raise
