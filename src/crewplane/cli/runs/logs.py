"""Select one exact saved log from persisted invocation and resume evidence."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import typer

from crewplane.architecture.safe_files import (
    contained_regular_file,
    is_safe_relative_path,
    path_has_symlink_component,
)
from crewplane.artifacts.naming import run_event_log_relative_path
from crewplane.artifacts.run_history import RunHistoryRecord, RunHistoryScan
from crewplane.artifacts.run_history_sources import (
    resolve_checkpoint_source,
    resolve_completed_node_source,
)
from crewplane.observability.events import (
    EventType,
    ExecutionEvent,
    is_invocation_event_type,
)
from crewplane.observability.events.reader import inspect_event_log


class SavedLogSelectionError(ValueError):
    """Saved evidence cannot establish a single safe provider log."""


@dataclass(frozen=True)
class InvocationLog:
    """Lifecycle evidence grouped by node, task, provider, audit round, and round.

    The log reference is retained without filesystem validation. ``started_at``
    is the greatest recorded start timestamp, or None when no start was seen.
    ``event_order`` is the zero-based index of that start in the supplied events,
    with later indices breaking timestamp ties. Without a start it is the latest
    matching lifecycle event's index, or -1 before any events are accumulated.
    Terminal events never replace a recorded start's timestamp or order.
    """

    node_id: str
    task_id: str
    provider: str
    audit_round_num: int | None
    round_num: int
    log_file: str | None = None
    started_at: float | None = None
    event_order: int = -1


@dataclass(frozen=True)
class SavedLogSelection:
    """A requested run's log path and the provenance run supplying its evidence.

    ``invocation`` is None for raw events, whose path is returned unchecked.
    Provider log paths are validated during selection. No file is held open;
    streaming must revalidate the path before reading it.
    """

    requested: RunHistoryRecord
    source: RunHistoryRecord
    path: Path
    invocation: InvocationLog | None = None


def select_saved_log(
    record: RunHistoryRecord, node_id: str | None, history: RunHistoryScan
) -> SavedLogSelection:
    """Select raw events or the latest captured log for an exact logical node.

    With no node ID, return the requested run's event path without validation.
    Otherwise resolve completed-node reuse before inspecting events, then rank
    invocations with ``latest_invocation_log`` and validate the chosen path.
    Follow checkpoint provenance only when reliable current evidence contains
    no invocation with a log reference; invalid evidence never triggers fallback.
    Selection emits no output and does not modify artifacts.

    Raises:
        SavedLogSelectionError: The node ID is blank, evidence is cyclic,
            incomplete, conflicting, or absent, or the chosen log is unsafe or
            missing.
        RunHistorySourceError: Manifest provenance is unavailable, inconsistent,
            or cyclic.
    """
    if node_id is None:
        return SavedLogSelection(
            record, record, record.run_dir / run_event_log_relative_path()
        )
    if not node_id.strip():
        raise SavedLogSelectionError("NODE must be an exact, nonempty logical node ID.")
    source = record
    visited: set[str] = set()
    while True:
        source = resolve_completed_node_source(source, node_id, history)
        key = source.manifest.run_key_name
        if key in visited:
            raise SavedLogSelectionError(
                f"Cyclic source evidence for node {node_id!r}."
            )
        visited.add(key)
        events = _inspect_node_log_evidence(source, node_id)
        invocation = latest_invocation_log(events, node_id)
        if invocation is not None:
            assert invocation.log_file is not None
            path = validate_log_reference(source, invocation.log_file)
            return SavedLogSelection(record, source, path, invocation)
        checkpoint = resolve_checkpoint_source(source, node_id, history)
        if checkpoint is None:
            raise _missing_log_evidence_error(source, node_id, events)
        source = checkpoint


def _inspect_node_log_evidence(
    source: RunHistoryRecord, node_id: str
) -> tuple[ExecutionEvent, ...]:
    inspection = inspect_event_log(
        source.run_dir / run_event_log_relative_path(),
        source.manifest.workflow_name,
        source.manifest.run_id,
    )
    if not inspection.ordering_reliable:
        key = source.manifest.run_key_name
        reasons = ", ".join(
            dict.fromkeys(item.reason for item in inspection.diagnostics)
        )
        raise SavedLogSelectionError(
            f"Cannot safely choose the latest invocation for node {node_id!r}: "
            f"evidence incomplete in {key} ({reasons}). Retry when the saved "
            "event log is complete, or inspect it with crewplane runs logs "
            f"{key} without --node."
        )
    return inspection.events


def _missing_log_evidence_error(
    source: RunHistoryRecord, node_id: str, events: tuple[ExecutionEvent, ...]
) -> SavedLogSelectionError:
    observed = sorted(
        {event.context.node_id for event in events if event.context.node_id}
    )
    suffix = (
        f" Observed node IDs: {', '.join(observed)}."
        if observed
        else " No node IDs were recorded."
    )
    return SavedLogSelectionError(
        f"No saved provider log evidence for node {node_id!r} in "
        f"{source.manifest.run_key_name}. "
        "The node may be unknown, pending, input-only, or have no captured output."
        + suffix
    )


def latest_invocation_log(
    events: tuple[ExecutionEvent, ...], node_id: str
) -> InvocationLog | None:
    """Rank one exact node's accumulated invocations with nonempty log references.

    Starts rank by timestamp, then their index in ``events``. A terminal-only
    invocation wins when its latest event follows the selected start, or no
    referenced start exists. Finish timestamps do not rank candidates. Return
    None when no matching invocation has a reference; do not inspect files.

    Raises:
        SavedLogSelectionError: A matching invocation lacks task, provider, or
            round coordinates, or its lifecycle records disagree on a log path.
            These checks apply even to invocations that would not be selected.
    """
    references = [
        item for item in _collect_invocation_logs(events, node_id) if item.log_file
    ]
    started = [item for item in references if item.started_at is not None]
    terminal_only = [item for item in references if item.started_at is None]
    newest = max(started, key=_start_order, default=None)
    fallback = max(terminal_only, key=lambda item: item.event_order, default=None)
    if fallback is not None and (
        newest is None or fallback.event_order > newest.event_order
    ):
        return fallback
    return newest


def _collect_invocation_logs(
    events: tuple[ExecutionEvent, ...], node_id: str
) -> tuple[InvocationLog, ...]:
    invocations: dict[tuple[str, str, str, int | None, int], InvocationLog] = {}
    for order, event in enumerate(events):
        if event.context.node_id != node_id or not is_invocation_event_type(
            event.event_type
        ):
            continue
        context = event.context
        if (
            context.task_id is None
            or context.provider is None
            or context.round_num is None
        ):
            raise SavedLogSelectionError(
                "Incomplete invocation coordinates; retry inspection."
            )
        identity = (
            node_id,
            context.task_id,
            context.provider,
            context.audit_round_num,
            context.round_num,
        )
        invocation = invocations.get(identity, InvocationLog(*identity))
        invocations[identity] = _retain_reference(invocation, event, order)
    return tuple(invocations.values())


def _start_order(invocation: InvocationLog) -> tuple[float, int]:
    assert invocation.started_at is not None
    return invocation.started_at, invocation.event_order


def _retain_reference(
    invocation: InvocationLog, event: ExecutionEvent, order: int
) -> InvocationLog:
    reference = event.context.log_file
    if reference is not None:
        if invocation.log_file is not None and reference != invocation.log_file:
            raise SavedLogSelectionError(
                "Conflicting log references for one saved invocation; cannot select safely."
            )
        invocation = replace(invocation, log_file=reference)
    if event.event_type == EventType.INVOCATION_STARTED:
        if invocation.started_at is None or (event.timestamp, order) > _start_order(
            invocation
        ):
            return replace(invocation, started_at=event.timestamp, event_order=order)
    elif invocation.started_at is None:
        return replace(invocation, event_order=order)
    return invocation


def validate_log_reference(record: RunHistoryRecord, reference: str) -> Path:
    """Return an absolute path to an existing, contained, single-link regular log.

    Normalize references within the evidence run before filesystem checks;
    reject traversal and symlink components. Absolute references must already
    belong to this run and are never rebased. No file remains open and no
    artifacts are modified.

    Raises:
        SavedLogSelectionError: The reference is invalid, unsafe, or missing.
            Outside-run normalization and unsafe filesystem failures retain their
            original ValueError or OSError cause; missing logs remain distinct.
    """
    root, relative = _normalize_log_reference(record, reference)
    selected = root / relative
    try:
        if path_has_symlink_component(selected):
            raise ValueError("symlink component")
        safe = contained_regular_file(root, relative)
        if safe is None:
            if selected.exists():
                raise ValueError("not a single-link regular file")
            raise SavedLogSelectionError(
                f"Saved event references a missing provider log: {selected}"
            )
    except (OSError, ValueError) as exc:
        if isinstance(exc, SavedLogSelectionError):
            raise
        raise SavedLogSelectionError(
            f"Cannot safely read saved provider log: {selected}"
        ) from exc
    return safe.absolute()


def _normalize_log_reference(
    record: RunHistoryRecord, reference: str
) -> tuple[Path, str]:
    path = Path(reference)
    if ".." in path.parts or "\\" in reference:
        raise SavedLogSelectionError(
            "Saved log reference contains unsafe traversal or separators."
        )
    root = record.run_dir.absolute()
    try:
        relative = (
            path.relative_to(root).as_posix() if path.is_absolute() else reference
        )
    except ValueError as exc:
        raise SavedLogSelectionError(
            f"Saved log reference is outside its evidence run: {reference}. "
            "Archived absolute paths are not rebased."
        ) from exc
    if not is_safe_relative_path(relative):
        raise SavedLogSelectionError(
            f"Saved log reference is not a safe contained path: {reference}"
        )
    return root, relative


def describe_selected_log(selection: SavedLogSelection) -> None:
    """Write selection details to stderr without reading logs or writing stdout.

    Emit requested/source run keys only when they differ, then invocation
    coordinates when present, and finally the log path. Missing audit rounds
    display as ``n/a``. Return None.
    """
    if (
        selection.source.manifest.run_key_name
        != selection.requested.manifest.run_key_name
    ):
        typer.echo(
            f"Requested run: {selection.requested.manifest.run_key_name}\n"
            f"Source evidence run: {selection.source.manifest.run_key_name}",
            err=True,
        )
    invocation = selection.invocation
    if invocation is not None:
        typer.echo(
            f"Node: {invocation.node_id}; task: {invocation.task_id}; provider: "
            f"{invocation.provider}; audit-round: {invocation.audit_round_num if invocation.audit_round_num is not None else 'n/a'}; "
            f"round: {invocation.round_num}",
            err=True,
        )
    typer.echo(f"Log: {selection.path}", err=True)
