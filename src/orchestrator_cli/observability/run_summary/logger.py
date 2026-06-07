from __future__ import annotations

from threading import Lock

from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.observability.events import (
    ExecutionEvent,
    format_execution_event_log_line,
    runtime_log_event,
)
from orchestrator_cli.observability.types import (
    DashboardSnapshot,
    RunContext,
    RunResult,
)

from .builder import build_run_summary
from .markdown import render_run_summary_markdown
from .models import PersistentLoggerLifecycle, RunSummary


class PersistentRunLogger:
    """Persist execution events and a compact summary for each workflow run."""

    required = True
    cleanup_after_start_timeout = False
    synchronous_snapshot_delivery = True

    def __init__(self, artifact_store: ArtifactStorePort) -> None:
        self._artifact_store = artifact_store
        self._event_log_path = artifact_store.get_orchestrator_event_log_path()
        self._summary_path = artifact_store.get_orchestrator_summary_path()
        self._lock = Lock()
        self._events: list[ExecutionEvent] = []
        self._latest_snapshot: DashboardSnapshot | None = None
        self._lifecycle = PersistentLoggerLifecycle.NEW
        self._workflow_name = artifact_store.task_name
        self._run_id = artifact_store.run_id
        self._last_summary: RunSummary | None = None

    def start(self, context: RunContext) -> None:
        if self._lifecycle != PersistentLoggerLifecycle.NEW:
            raise RuntimeError("Persistent run logger cannot be restarted.")
        self._workflow_name = context.workflow_topology.workflow_name
        self._run_id = context.run_id
        self._event_log_path.parent.mkdir(parents=True, exist_ok=True)
        self._event_log_path.write_text("", encoding="utf-8")
        with self._lock:
            self._events = []
            self._latest_snapshot = None
            self._last_summary = None
            self._lifecycle = PersistentLoggerLifecycle.RUNNING

    @property
    def started(self) -> bool:
        return self._lifecycle == PersistentLoggerLifecycle.RUNNING

    @property
    def last_summary(self) -> RunSummary | None:
        with self._lock:
            return self._last_summary

    def refresh_summary(self, result: RunResult) -> RunSummary | None:
        with self._lock:
            if self._lifecycle == PersistentLoggerLifecycle.NEW:
                return None
            snapshot = self._latest_snapshot
            events = list(self._events)
        summary = build_run_summary(
            artifact_store=self._artifact_store,
            snapshot=snapshot,
            events=events,
            result=result,
            fallback_workflow_name=self._workflow_name,
            fallback_run_id=self._run_id,
        )
        self._summary_path.parent.mkdir(parents=True, exist_ok=True)
        self._summary_path.write_text(
            render_run_summary_markdown(summary),
            encoding="utf-8",
        )
        with self._lock:
            self._last_summary = summary
        return summary

    def on_snapshot(
        self,
        event: ExecutionEvent | None,
        snapshot: DashboardSnapshot,
    ) -> None:
        with self._lock:
            if self._lifecycle != PersistentLoggerLifecycle.RUNNING:
                return
            self._latest_snapshot = snapshot
            if event is None:
                return
            self._events.append(event)
            with self._event_log_path.open("a", encoding="utf-8") as handle:
                handle.write(format_execution_event_log_line(event))

    def stop(self, result: RunResult) -> None:
        if self._lifecycle == PersistentLoggerLifecycle.NEW:
            return
        if self._lifecycle == PersistentLoggerLifecycle.STOPPED:
            return
        try:
            self.refresh_summary(result)
        finally:
            with self._lock:
                self._lifecycle = PersistentLoggerLifecycle.STOPPED

    def record_event(self, event: ExecutionEvent) -> None:
        self._append_event(event, allow_stopped=False)

    def record_failure_summary_event(
        self,
        workflow_name: str,
        run_id: str,
        message: str,
    ) -> None:
        self._append_event(
            runtime_log_event(
                workflow_name=workflow_name,
                run_id=run_id,
                level="error",
                message=message,
                operation="runtime_error",
            ),
            allow_stopped=True,
        )

    def _append_event(self, event: ExecutionEvent, allow_stopped: bool) -> None:
        with self._lock:
            allowed_lifecycles = {PersistentLoggerLifecycle.RUNNING}
            if allow_stopped:
                allowed_lifecycles.add(PersistentLoggerLifecycle.STOPPED)
            if self._lifecycle not in allowed_lifecycles:
                return
            self._events.append(event)
            self._event_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._event_log_path.open("a", encoding="utf-8") as handle:
                handle.write(format_execution_event_log_line(event))
