from __future__ import annotations

import os
from collections import deque
from pathlib import Path
from threading import Lock
from typing import cast

from crewplane.architecture.contracts import (
    DashboardSnapshot as PublicDashboardSnapshot,
)
from crewplane.architecture.contracts import ObserverCapabilities
from crewplane.architecture.ports import ArtifactStorePort
from crewplane.architecture.safe_files import ensure_single_link_regular_file
from crewplane.artifacts.atomic import atomic_write_text
from crewplane.observability.events import (
    ExecutionEvent,
    format_execution_event_log_line,
    read_event_log,
    runtime_log_event,
)
from crewplane.observability.types import (
    DashboardSnapshot,
    RunContext,
    RunResult,
)

from .accumulator import RunSummaryAccumulator
from .builder import build_run_summary
from .markdown import render_run_summary_markdown
from .models import (
    PersistentLoggerLifecycle,
    ProviderTokenAggregates,
    RunSummary,
)
from .spend import provider_token_aggregates

MAX_RETAINED_SUMMARY_EVENTS = 2_000
_TERMINAL_EVENT_TYPES = frozenset(
    {"workflow_finished", "workflow_failed", "workflow_cancelled"}
)


class PersistentRunLogger:
    """Persist execution events and a compact summary for each workflow run."""

    capabilities = ObserverCapabilities(
        required=True,
        cleanup_after_start_timeout=False,
        synchronous_snapshot_delivery=True,
    )

    @property
    def stop_requested(self) -> bool:
        return False

    def __init__(self, artifact_store: ArtifactStorePort) -> None:
        self._artifact_store = artifact_store
        self._event_log_path = artifact_store.get_run_event_log_path()
        self._summary_path = artifact_store.get_run_summary_path()
        self._lock = Lock()
        self._summary_publication_lock = Lock()
        self._exact_token_summary_published = False
        self._events: deque[ExecutionEvent] = deque(maxlen=MAX_RETAINED_SUMMARY_EVENTS)
        self._dropped_event_count = 0
        self._latest_snapshot: DashboardSnapshot | None = None
        self._lifecycle = PersistentLoggerLifecycle.NEW
        self._workflow_name = artifact_store.task_name
        self._run_id = artifact_store.run_id
        self._last_summary: RunSummary | None = None
        self._summary_accumulator = RunSummaryAccumulator()

    def start(self, context: RunContext) -> None:
        if self._lifecycle != PersistentLoggerLifecycle.NEW:
            raise RuntimeError("Persistent run logger cannot be restarted.")
        self._workflow_name = context.workflow_topology.workflow_name
        self._run_id = context.run_id
        event_log_path = ensure_single_link_regular_file(self._event_log_path)
        atomic_write_text(event_log_path, "", ensure_parent=False)
        with self._lock:
            self._events = deque(maxlen=MAX_RETAINED_SUMMARY_EVENTS)
            self._dropped_event_count = 0
            self._latest_snapshot = None
            self._last_summary = None
            self._summary_accumulator = RunSummaryAccumulator()
            self._lifecycle = PersistentLoggerLifecycle.RUNNING

    @property
    def started(self) -> bool:
        return self._lifecycle == PersistentLoggerLifecycle.RUNNING

    @property
    def last_summary(self) -> RunSummary | None:
        with self._lock:
            return self._last_summary

    @property
    def retained_event_count(self) -> int:
        with self._lock:
            return len(self._events)

    @property
    def dropped_event_count(self) -> int:
        with self._lock:
            return self._dropped_event_count

    def refresh_summary(self, result: RunResult) -> RunSummary | None:
        with self._lock:
            if self._lifecycle == PersistentLoggerLifecycle.NEW:
                return None
        durable_events = read_event_log(self._event_log_path)
        return self._write_summary(
            result,
            provider_token_aggregates(durable_events),
            token_aggregates_are_exact=True,
        )

    def _write_summary(
        self,
        result: RunResult,
        token_aggregates: ProviderTokenAggregates,
        token_aggregates_are_exact: bool,
    ) -> RunSummary | None:
        summary = self._build_summary(result, token_aggregates)
        if summary is None:
            return None
        return self._publish_summary(summary, token_aggregates_are_exact)

    def _build_summary(
        self,
        result: RunResult,
        token_aggregates: ProviderTokenAggregates,
    ) -> RunSummary | None:
        with self._lock:
            if self._lifecycle == PersistentLoggerLifecycle.NEW:
                return None
            snapshot = self._latest_snapshot
            events = list(self._events)
            dropped_event_count = self._dropped_event_count
            summary_facts = self._summary_accumulator.snapshot()
        return build_run_summary(
            artifact_store=self._artifact_store,
            snapshot=snapshot,
            events=events,
            dropped_event_count=dropped_event_count,
            result=result,
            fallback_workflow_name=self._workflow_name,
            fallback_run_id=self._run_id,
            summary_facts=summary_facts,
            token_aggregates=token_aggregates,
        )

    def _publish_summary(
        self,
        summary: RunSummary,
        token_aggregates_are_exact: bool,
    ) -> RunSummary | None:
        summary_text = render_run_summary_markdown(summary)
        with self._summary_publication_lock:
            if self._exact_token_summary_published and not token_aggregates_are_exact:
                return self.last_summary
            atomic_write_text(self._summary_path, summary_text)
            with self._lock:
                self._last_summary = summary
            if token_aggregates_are_exact:
                self._exact_token_summary_published = True
        return summary

    def on_snapshot(
        self,
        event: ExecutionEvent | None,
        snapshot: PublicDashboardSnapshot,
    ) -> None:
        with self._lock:
            if self._lifecycle != PersistentLoggerLifecycle.RUNNING:
                return
            self._latest_snapshot = cast(DashboardSnapshot, snapshot)
            if event is None:
                return
            event_log_path = ensure_single_link_regular_file(self._event_log_path)
            event_line = format_execution_event_log_line(event)
            if event.event_type in _TERMINAL_EVENT_TYPES and _event_line_is_durable(
                event_log_path,
                event_line,
            ):
                return
            self._record_event_summary(event)
            _append_event_log_line(
                event_log_path,
                event_line,
            )

    def stop(self, result: RunResult) -> None:
        if self._lifecycle == PersistentLoggerLifecycle.NEW:
            return
        if self._lifecycle == PersistentLoggerLifecycle.STOPPED:
            return
        try:
            self._write_summary(
                result,
                ProviderTokenAggregates(),
                token_aggregates_are_exact=False,
            )
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
            self._record_event_summary(event)
            event_log_path = ensure_single_link_regular_file(self._event_log_path)
            _append_event_log_line(
                event_log_path,
                format_execution_event_log_line(event),
            )

    def _record_event_summary(self, event: ExecutionEvent) -> None:
        self._summary_accumulator.record(event)
        if len(self._events) == MAX_RETAINED_SUMMARY_EVENTS:
            self._dropped_event_count += 1
        self._events.append(event)


def _append_event_log_line(path: Path, line: str) -> None:
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(line)


def _event_line_is_durable(path: Path, line: str) -> bool:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    expected_line = line.encode("utf-8")
    with os.fdopen(descriptor, "rb") as handle:
        return any(durable_line == expected_line for durable_line in handle)
