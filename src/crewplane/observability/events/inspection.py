"""Read-only evidence validation, separate from tolerant event decoding."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from crewplane.architecture.safe_file_reads import open_regular_file
from crewplane.architecture.safe_files import (
    is_single_link_regular_file,
    path_has_symlink_component,
)
from crewplane.observability.events.execution_event import ExecutionEvent
from crewplane.observability.events.payloads import RuntimeLogEventPayload
from crewplane.observability.events.types import is_invocation_event_type

type EventLogDiagnosticCategory = Literal[
    "missing",
    "unreadable",
    "unsafe",
    "encoding",
    "malformed",
    "identity",
    "invalid",
    "partial",
]


@dataclass(frozen=True)
class EventLogDiagnostic:
    """A saved event-log problem with a one-based line number when known."""

    category: EventLogDiagnosticCategory
    reason: str
    line_number: int | None = None


@dataclass(frozen=True)
class EventLogInspection:
    """Validated snapshot events in file order and diagnostics for rejected evidence.

    Completeness describes the captured evidence, not workflow completion.
    """

    events: tuple[ExecutionEvent, ...] = ()
    diagnostics: tuple[EventLogDiagnostic, ...] = ()

    @property
    def complete(self) -> bool:
        """Whether the snapshot has no diagnostics."""
        return not self.diagnostics

    @property
    def partial(self) -> bool:
        """Whether the snapshot contains an unfinished trailing event append."""
        return any(item.category == "partial" for item in self.diagnostics)

    @property
    def ordering_reliable(self) -> bool:
        """Whether event ordering can be trusted; follows completeness exactly."""
        return self.complete


def read_event_snapshot(path: Path) -> bytes:
    """Capture initial bytes only; ordinary append does not invalidate evidence."""
    if path_has_symlink_component(path):
        raise ValueError("Event log path contains a symlink.")
    with open_regular_file(path) as descriptor:
        initial = os.fstat(descriptor)
        _validate_snapshot_path(path, initial, initial)
        remaining = initial.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError(
                    "Event log truncated during snapshot; retry inspection."
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        _validate_snapshot_path(path, initial, os.fstat(descriptor))
        return b"".join(chunks)


def _validate_snapshot_path(
    path: Path, initial: os.stat_result, opened: os.stat_result
) -> None:
    current = path.lstat()
    if path_has_symlink_component(path) or any(
        not is_single_link_regular_file(metadata)
        or (metadata.st_dev, metadata.st_ino) != (initial.st_dev, initial.st_ino)
        or metadata.st_size < initial.st_size
        for metadata in (opened, current)
    ):
        raise ValueError("Event log changed during snapshot; retry inspection.")


def inspection_event_problem(
    event: ExecutionEvent, record: Mapping[str, object]
) -> str | None:
    """Validate fields which the historical decoder deliberately tolerates."""
    problem = _timestamp_problem(event)
    if (
        problem is None
        and isinstance(event.payload, RuntimeLogEventPayload)
        and record.get("attributes") is not None
        and event.payload.attributes is None
    ):
        return "runtime log attributes"
    if problem is not None or not is_invocation_event_type(event.event_type):
        return problem
    return _invocation_log_reference_problem(record) or _invocation_coordinates_problem(
        event, record
    )


def _timestamp_problem(event: ExecutionEvent) -> str | None:
    try:
        datetime.fromisoformat(event.timestamp_utc).timestamp()
    except (ValueError, OverflowError, OSError):
        return "timestamp"
    return None


def _invocation_log_reference_problem(record: Mapping[str, object]) -> str | None:
    reference = record.get("log_file")
    if reference is not None and (
        not isinstance(reference, str) or not reference.strip()
    ):
        return "invocation log reference"
    return None


def _invocation_coordinates_problem(
    event: ExecutionEvent, record: Mapping[str, object]
) -> str | None:
    context = event.context
    if any(
        not isinstance(value, str) or not value.strip()
        for value in (context.node_id, context.task_id, context.provider)
    ):
        return "invocation coordinates"
    if (
        type(record.get("round_num")) is not int
        or context.round_num is None
        or context.round_num < 0
    ):
        return "invocation coordinates"
    audit_round = record.get("audit_round_num")
    if audit_round is not None and (
        type(audit_round) is not int
        or context.audit_round_num is None
        or context.audit_round_num < 1
    ):
        return "invocation coordinates"
    return None
