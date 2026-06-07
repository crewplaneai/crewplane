from __future__ import annotations

import json

from orchestrator_cli.observability.events.execution_event import ExecutionEvent


def execution_event_log_record(event: ExecutionEvent) -> dict[str, object]:
    record: dict[str, object] = {
        "event_type": event.event_type,
        "run_id": event.run_id,
        "timestamp": event.timestamp_utc,
        "workflow_name": event.workflow_name,
    }
    for key, value in event.context.as_event_fields().items():
        if value is None:
            continue
        record[key] = value

    for key, value in event.payload.as_event_fields().items():
        if value is None:
            continue
        record[key] = value
    return record


def format_execution_event_log_line(event: ExecutionEvent) -> str:
    return json.dumps(execution_event_log_record(event), sort_keys=True) + "\n"
