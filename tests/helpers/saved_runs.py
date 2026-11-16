"""Filesystem-local saved execution scenarios shared by CLI tests."""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from crewplane.artifacts.run_history import RunHistoryRecord
from crewplane.observability.events import (
    EventType,
    ExecutionEvent,
    format_execution_event_log_line,
    is_invocation_event_type,
)
from tests.helpers.observability import make_execution_event
from tests.helpers.resume import make_run_manifest, write_run_manifest


def saved_run(
    state_dir: Path,
    run_id: str = "run-1",
    key: str = "workflow--run-1",
    workflow_name: str = "Workflow",
    status: str = "succeeded",
    started_offset: int = 0,
    **updates: Any,
) -> RunHistoryRecord:
    manifest = make_run_manifest(
        run_id, key, status, started_offset, workflow_name=workflow_name
    ).model_copy(update=updates)
    path = write_run_manifest(state_dir, manifest)
    return RunHistoryRecord(
        manifest, path, path.parent.parent, state_dir / "execution-results" / key
    )


def saved_event(
    record: RunHistoryRecord,
    kind: EventType = EventType.WORKFLOW_FINISHED,
    **fields: Any,
) -> ExecutionEvent:
    values: dict[str, Any] = {
        "workflow_name": record.manifest.workflow_name,
        "run_id": record.manifest.run_id,
        "timestamp_utc": "2026-06-09T12:00:00+00:00",
        **fields,
    }
    if is_invocation_event_type(kind):
        values.setdefault("role", "executor")
    with suppress(ValueError):
        values.setdefault(
            "timestamp", datetime.fromisoformat(values["timestamp_utc"]).timestamp()
        )
    return make_execution_event(kind, **values)


def write_events(record: RunHistoryRecord, events: list[ExecutionEvent]) -> Path:
    path = record.run_dir / "logs/events.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        "".join(format_execution_event_log_line(event) for event in events).encode(
            "utf-8"
        )
    )
    return path
