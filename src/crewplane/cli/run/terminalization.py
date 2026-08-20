from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast

from crewplane.architecture.contracts import RunResult
from crewplane.architecture.ports import ArtifactStorePort
from crewplane.artifacts.locks.manifest import (
    TERMINAL_RECOVERY_PHASES,
    TerminalRecoveryPhase,
)
from crewplane.core.execution_state import TerminalRunStatus
from crewplane.observability.events import (
    ExecutionEvent,
    WorkflowEventType,
    read_event_log,
    workflow_event,
)
from crewplane.observability.run_summary.logger import PersistentRunLogger

from .manifest import finalize_run_manifest

_TERMINAL_EVENT_TYPE_BY_STATUS: dict[TerminalRunStatus, WorkflowEventType] = {
    "succeeded": "workflow_finished",
    "failed": "workflow_failed",
    "cancelled": "workflow_cancelled",
}


class TerminalizationHub(Protocol):
    def emit(self, event: ExecutionEvent) -> None: ...

    def set_terminal_result(self, result: RunResult) -> None: ...


class TerminalRecoveryRecorder(Protocol):
    def record_terminal_recovery(
        self,
        phase: TerminalRecoveryPhase,
        status: TerminalRunStatus,
        reason: str | None,
    ) -> None: ...


class TerminalizationCommitter(Protocol):
    def commit(
        self,
        hub: TerminalizationHub,
        status: TerminalRunStatus,
        reason: str | None = None,
    ) -> None: ...


@dataclass
class TerminalizationCoordinator:
    """Publish the one terminal outcome shared by durable and live consumers."""

    output: ArtifactStorePort
    workflow_name: str
    terminal_recovery_recorder: TerminalRecoveryRecorder | None = None
    recovery_phase: TerminalRecoveryPhase | None = None
    manifest_published: bool = False
    event_published: bool = False
    result_published: bool = False
    summary_published: bool = False
    committed: bool = False
    summary_logger: PersistentRunLogger | None = None
    _status: TerminalRunStatus | None = None
    _reason: str | None = None
    _event: ExecutionEvent | None = None
    _result: RunResult | None = None

    def bind_summary_logger(self, logger: PersistentRunLogger) -> None:
        if self.committed:
            raise RuntimeError("Cannot bind a summary logger after terminalization.")
        self.summary_logger = logger

    def commit(
        self,
        hub: TerminalizationHub,
        status: TerminalRunStatus,
        reason: str | None = None,
    ) -> None:
        if self.committed:
            return
        if self.summary_logger is None:
            raise RuntimeError("Terminalization requires a bound summary logger.")
        normalized_reason = self._normalize_terminal_outcome(status, reason)
        self._set_outcome(status, normalized_reason)
        self._publish_recovery_phase("outcome_selected")
        self._publish_terminal_event(hub, status, normalized_reason)
        self._publish_terminal_result(hub, status, normalized_reason)
        self._publish_summary()
        self._publish_recovery_phase("terminal_views_published")

    @property
    def publication_complete(self) -> bool:
        return all(
            (
                self.manifest_published,
                self.event_published,
                self.result_published,
                self.summary_published,
                self.recovery_phase == "observer_shutdown_complete"
                or self.terminal_recovery_recorder is None,
            )
        )

    def acknowledge_observer_shutdown(self) -> None:
        """Publish the terminal commit marker after required observers stop."""

        if self.committed:
            return
        if not self._observer_publications_complete:
            raise RuntimeError(
                "Terminalization cannot commit before observer publications complete."
            )
        self._publish_observer_shutdown_with_retry()
        self._publish_manifest_with_retry()
        self.committed = True

    @property
    def _observer_publications_complete(self) -> bool:
        return all(
            (
                self.event_published,
                self.result_published,
                self.summary_published,
            )
        )

    def _publish_manifest_with_retry(self) -> None:
        if self.manifest_published:
            return
        _retry_once(self._publish_manifest, "manifest publication")

    def _publish_observer_shutdown_with_retry(self) -> None:
        _retry_once(
            self._publish_observer_shutdown,
            "terminal recovery publication",
        )

    def _publish_observer_shutdown(self) -> None:
        self._publish_recovery_phase("observer_shutdown_complete")

    def _publish_recovery_phase(self, phase: TerminalRecoveryPhase) -> None:
        if self.terminal_recovery_recorder is None:
            return
        if self._status is None:
            raise RuntimeError("Terminalization outcome has not been selected.")
        if self.recovery_phase is not None:
            current_index = TERMINAL_RECOVERY_PHASES.index(self.recovery_phase)
            requested_index = TERMINAL_RECOVERY_PHASES.index(phase)
            if requested_index <= current_index:
                return
        self.terminal_recovery_recorder.record_terminal_recovery(
            phase,
            self._status,
            self._reason,
        )
        self.recovery_phase = phase

    def _publish_manifest(self) -> None:
        if self._status is None:
            raise RuntimeError("Terminalization outcome has not been selected.")
        finalize_run_manifest(
            self.output,
            self._status,
            failure_message=self._reason if self._status == "failed" else None,
            cancel_reason=self._reason if self._status == "cancelled" else None,
        )
        self.manifest_published = True

    def _normalize_terminal_outcome(
        self,
        status: TerminalRunStatus,
        reason: str | None,
    ) -> str | None:
        normalized_reason = (reason or "").strip() or None
        if status == "succeeded" and normalized_reason is not None:
            raise ValueError("Successful terminalization cannot include a reason.")
        if status == "failed" and normalized_reason is None:
            return "Workflow execution failed."
        if status == "cancelled" and normalized_reason is None:
            return "Workflow execution was cancelled."
        return normalized_reason

    def _set_outcome(
        self,
        status: TerminalRunStatus,
        reason: str | None,
    ) -> None:
        if self._status is None:
            self._status = status
            self._reason = reason
            return
        if self._status != status or self._reason != reason:
            raise RuntimeError("Run terminalization outcome cannot change on retry.")

    def _publish_terminal_event(
        self,
        hub: TerminalizationHub,
        status: TerminalRunStatus,
        reason: str | None,
    ) -> None:
        if self._event is None:
            self._event = workflow_event(
                _TERMINAL_EVENT_TYPE_BY_STATUS[status],
                workflow_name=self.workflow_name,
                run_id=self.output.run_id,
                error=reason,
            )
        if self.event_published:
            return
        hub.emit(self._event)
        if not self._event_is_durable():
            raise RuntimeError(
                "Terminal event publication did not produce durable evidence."
            )
        self.event_published = True

    def _publish_terminal_result(
        self,
        hub: TerminalizationHub,
        status: TerminalRunStatus,
        reason: str | None,
    ) -> None:
        if self._result is None:
            self._result = RunResult(
                status=status,
                cancel_reason=reason if status == "cancelled" else None,
            )
        if self.result_published:
            return
        hub.set_terminal_result(self._result)
        self.result_published = True

    def _publish_summary(self) -> None:
        if self.summary_published:
            return
        if self._result is None:
            raise RuntimeError("Terminal result has not been prepared.")
        cast(PersistentRunLogger, self.summary_logger).refresh_summary(self._result)
        self.summary_published = True

    def _event_is_durable(self) -> bool:
        if self._event is None:
            return False
        return any(
            event.event_type == self._event.event_type
            and event.workflow_name == self._event.workflow_name
            and event.run_id == self._event.run_id
            and event.context == self._event.context
            and event.payload == self._event.payload
            and event.timestamp_utc == self._event.timestamp_utc
            for event in read_event_log(self.output.get_run_event_log_path())
        )


def _retry_once(action: Callable[[], None], operation: str) -> None:
    try:
        action()
    except Exception as first_error:
        try:
            action()
        except Exception as retry_error:
            retry_error.add_note(f"{operation} retry failed after: {first_error}")
            raise


def commit_terminalization_with_retry(
    coordinator: TerminalizationCommitter,
    hub: TerminalizationHub,
    status: TerminalRunStatus,
    reason: str | None = None,
) -> None:
    """Retry an incomplete terminal publication once after a transient failure."""

    try:
        coordinator.commit(hub, status, reason)
    except Exception as first_error:
        try:
            coordinator.commit(hub, status, reason)
        except Exception as retry_error:
            retry_error.add_note(f"terminalization retry failed after: {first_error}")
            raise
