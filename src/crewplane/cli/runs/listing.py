"""Manifest-derived history rows with explicitly incomplete saved evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from crewplane.artifacts.naming import run_event_log_relative_path
from crewplane.artifacts.resume.checkpoint_store import read_review_checkpoint
from crewplane.artifacts.run_history import (
    RunHistoryRecord,
    RunHistoryScan,
    persisted_start_time,
)
from crewplane.artifacts.run_history_sources import (
    RunHistorySourceError,
    resolve_completed_node_source,
)
from crewplane.core.execution_state import ReviewCheckpointResumeSummary
from crewplane.core.review_checkpoint import OpenReviewCheckpoint
from crewplane.observability.events import (
    TERMINAL_WORKFLOW_EVENT_TYPES,
    EventType,
    ExecutionEvent,
)
from crewplane.observability.events.inspection import EventLogInspection
from crewplane.observability.events.reader import inspect_event_log
from crewplane.observability.run_summary.builder import run_summary_facts_from_events
from crewplane.observability.run_summary.formatting import format_duration
from crewplane.observability.run_summary.issues import issue_summaries

from .selection import run_selectors

_RUN_TABLE_HEADERS = (
    "Workflow",
    "Run",
    "Started",
    "Outcome",
    "Elapsed",
    "Reused",
    "Attention",
)


@dataclass(frozen=True)
class SavedRunRow:
    """Display values for one saved execution."""

    workflow: str
    selector: str
    started: str
    outcome: str
    elapsed: str
    reused: str
    attention: str


@dataclass(frozen=True)
class _ReviewEvidence:
    """Independent review-decision and completeness flags from saved evidence."""

    unresolved: bool
    incomplete: bool


def saved_run_rows(history: RunHistoryScan) -> tuple[SavedRunRow, ...]:
    """Build rows in history order, sharing event snapshots across runs and nodes.

    Unavailable evidence marks attention incomplete without hiding known flags.
    """
    selectors = run_selectors(history)
    evidence: dict[str, EventLogInspection] = {}
    rows: list[SavedRunRow] = []
    for record in history.records:
        manifest = record.manifest
        rows.append(
            SavedRunRow(
                workflow=manifest.workflow_name,
                selector=selectors[manifest.run_key_name],
                started=_recorded_start_label(manifest.started_at),
                outcome=(
                    "running (recorded)"
                    if manifest.status == "running"
                    else manifest.status
                ),
                elapsed=_manifest_elapsed(record),
                reused=_reuse_label(record),
                attention=run_attention(record, history, evidence),
            )
        )
    return tuple(rows)


def print_saved_runs(history: RunHistoryScan, console: Console) -> None:
    """Print saved runs with literal, folded cells and encoding-safe text.

    Keep every column in narrow output; print a message for empty history.
    """
    if not history.records:
        console.print("No saved execution runs found.")
        return
    # Retain all seven columns and wide Unicode even in an unusually narrow stream.
    table = Table(
        box=box.ASCII, show_lines=True, padding=(0, 1), width=max(36, console.width)
    )
    for heading in _RUN_TABLE_HEADERS:
        table.add_column(heading, overflow="fold", min_width=2)
    for row in saved_run_rows(history):
        values = (
            row.workflow,
            row.selector,
            row.started,
            row.outcome,
            row.elapsed,
            row.reused,
            row.attention,
        )
        table.add_row(*(_plain_text(value, console) for value in values))
    console.print(table, crop=False)


def _plain_text(value: str, console: Console) -> Text:
    visible = value.encode(console.encoding, errors="backslashreplace").decode(
        console.encoding
    )
    return Text(visible, overflow="fold")


def _recorded_start_label(value: str) -> str:
    """Label naive ISO timestamps as recorded local time; preserve aware text.

    Invalid ISO timestamps raise ValueError.
    """
    stamp = datetime.fromisoformat(value)
    return f"{value} (recorded local time)" if stamp.tzinfo is None else value


def _manifest_elapsed(record: RunHistoryRecord) -> str:
    """Format a finalized manifest interval, or n/a when unavailable.

    Mixed timezone awareness, unrepresentable instants, and negative durations
    are unavailable. Invalid ISO timestamps raise ValueError.
    """
    manifest = record.manifest
    if manifest.status == "running" or manifest.completed_at is None:
        return format_duration(None)
    start = datetime.fromisoformat(manifest.started_at)
    end = datetime.fromisoformat(manifest.completed_at)
    if (start.tzinfo is None) != (end.tzinfo is None):
        return format_duration(None)
    try:
        elapsed = (
            persisted_start_time(manifest.completed_at)
            - persisted_start_time(manifest.started_at)
        ).total_seconds()
    except (ValueError, OverflowError):
        return format_duration(None)
    return format_duration(elapsed if elapsed >= 0 else None)


def _reuse_label(record: RunHistoryRecord) -> str:
    """Format the counts of reused nodes and review checkpoints."""
    nodes = len(record.manifest.resumed_nodes)
    checkpoints = len(record.manifest.resumed_review_checkpoints)
    return (
        f"yes ({nodes} nodes, {checkpoints} checkpoints)"
        if nodes or checkpoints
        else "no"
    )


def run_attention(
    record: RunHistoryRecord,
    history: RunHistoryScan,
    evidence: dict[str, EventLogInspection],
) -> str:
    """Describe saved attention in outcome, error, review, incomplete order.

    Populate and reuse evidence snapshots by run key. Incomplete evidence does
    not clear known flags; recorded running status does not prove liveness.
    """
    inspection = _run_evidence(record, evidence)
    current = list(inspection.events)
    flags: list[str] = []
    if record.manifest.status in {"failed", "cancelled"}:
        flags.append(record.manifest.status)
    if any(issue.level == "error" for issue in issue_summaries(current)):
        flags.append("errors recorded")
    reviews = (
        _current_review_evidence(record, inspection),
        _reused_node_review_evidence(record, history, evidence),
        _checkpoint_review_evidence(record, current),
    )
    if any(review.unresolved for review in reviews):
        flags.append("review unresolved")
    if any(review.incomplete for review in reviews):
        flags.append("evidence incomplete")
    return ", ".join(flags) if flags else "none recorded"


def _current_review_evidence(
    record: RunHistoryRecord, inspection: EventLogInspection
) -> _ReviewEvidence:
    current = list(inspection.events)
    unresolved = run_summary_facts_from_events(current).review_consensus_unresolved
    incomplete = (
        not inspection.complete
        or record.manifest.status == "running"
        or not any(
            event.event_type in TERMINAL_WORKFLOW_EVENT_TYPES for event in current
        )
    )
    return _ReviewEvidence(unresolved=unresolved, incomplete=incomplete)


def _reused_node_review_evidence(
    record: RunHistoryRecord,
    history: RunHistoryScan,
    evidence: dict[str, EventLogInspection],
) -> _ReviewEvidence:
    unresolved = False
    incomplete = False
    for node in record.manifest.resumed_nodes:
        try:
            source = resolve_completed_node_source(record, node, history)
        except RunHistorySourceError:
            incomplete = True
            continue
        source_evidence = _run_evidence(source, evidence)
        node_events = [
            event for event in source_evidence.events if event.context.node_id == node
        ]
        unresolved |= run_summary_facts_from_events(
            node_events
        ).review_consensus_unresolved
        incomplete |= not source_evidence.complete or not any(
            event.event_type == EventType.NODE_FINISHED for event in node_events
        )
        checkpoint_evidence = _finalized_checkpoint_evidence(source, node)
        unresolved |= checkpoint_evidence.unresolved
        incomplete |= checkpoint_evidence.incomplete
    return _ReviewEvidence(unresolved=unresolved, incomplete=incomplete)


def _checkpoint_review_evidence(
    record: RunHistoryRecord, current: list[ExecutionEvent]
) -> _ReviewEvidence:
    unresolved = False
    incomplete = False
    for checkpoint in record.manifest.resumed_review_checkpoints:
        checkpoint_evidence = _finalized_checkpoint_evidence(record, checkpoint.node_id)
        unresolved |= checkpoint_evidence.unresolved
        incomplete |= checkpoint_evidence.incomplete
        incomplete |= not any(
            event.context.node_id == checkpoint.node_id
            and event.event_type
            in {EventType.NODE_FINISHED, EventType.NODE_FAILED, EventType.NODE_BLOCKED}
            for event in current
        )
    return _ReviewEvidence(unresolved=unresolved, incomplete=incomplete)


def _finalized_checkpoint_evidence(
    record: RunHistoryRecord, node_id: str
) -> _ReviewEvidence:
    """Read retained review evidence for a finalization checkpoint.

    No matching summary adds no flags. Unavailable checkpoint data marks the
    evidence incomplete.
    """
    summary = next(
        (
            item
            for item in record.manifest.resumed_review_checkpoints
            if item.node_id == node_id and item.source_phase == "finalize"
        ),
        None,
    )
    if summary is None:
        return _ReviewEvidence(unresolved=False, incomplete=False)
    checkpoint = _read_finalized_checkpoint(record, summary)
    if checkpoint is None:
        return _ReviewEvidence(unresolved=False, incomplete=True)
    unresolved = (
        checkpoint.progress.continued_after_stop
        and checkpoint.progress.stop_reason in {"consensus_exhausted", "no_progress"}
    )
    return _ReviewEvidence(unresolved=unresolved, incomplete=False)


def _read_finalized_checkpoint(
    record: RunHistoryRecord, summary: ReviewCheckpointResumeSummary
) -> OpenReviewCheckpoint | None:
    """Read a finalization marker matching the run and hydration provenance.

    Treat OSError and ValueError as unavailable evidence; other errors propagate.
    """
    try:
        checkpoint = read_review_checkpoint(record.run_dir, summary.node_id)
    except (OSError, ValueError):
        return None
    if not isinstance(checkpoint, OpenReviewCheckpoint) or not (
        _checkpoint_matches_provenance(checkpoint, record, summary)
    ):
        return None
    return checkpoint


def _checkpoint_matches_provenance(
    checkpoint: OpenReviewCheckpoint,
    record: RunHistoryRecord,
    summary: ReviewCheckpointResumeSummary,
) -> bool:
    """Match finalization identity and hydration provenance."""
    manifest = record.manifest
    return (
        checkpoint.next_phase == "finalize"
        and checkpoint.run_id == manifest.run_id
        and checkpoint.run_key_name == manifest.run_key_name
        and checkpoint.workflow_identity == manifest.workflow_identity
        and checkpoint.workflow_name == manifest.workflow_name
        and checkpoint.workflow_signature == manifest.workflow_signature
        and checkpoint.node_id == summary.node_id
        and checkpoint.resume_origin == summary.resume_origin
        and checkpoint.audit == summary.source_audit
        and checkpoint.local_round == summary.source_local_round
    )


def _run_evidence(
    record: RunHistoryRecord, cache: dict[str, EventLogInspection]
) -> EventLogInspection:
    key = record.manifest.run_key_name
    if key not in cache:
        cache[key] = inspect_event_log(
            record.run_dir / run_event_log_relative_path(),
            record.manifest.workflow_name,
            record.manifest.run_id,
        )
    return cache[key]
