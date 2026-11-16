"""Resolve node-scoped saved evidence using manifest provenance only."""

from __future__ import annotations

from crewplane.artifacts.run_history import RunHistoryRecord, RunHistoryScan


class RunHistorySourceError(ValueError):
    """Saved node provenance is missing, inconsistent, or cyclic."""


def resolve_completed_node_source(
    record: RunHistoryRecord, node_id: str, history: RunHistoryScan
) -> RunHistoryRecord:
    visited: set[str] = set()
    while node_id in record.manifest.resumed_nodes:
        key = record.manifest.run_key_name
        if key in visited:
            raise RunHistorySourceError(f"Cyclic source evidence for node {node_id}.")
        visited.add(key)
        record = _source_record(
            record,
            (
                record.manifest.resume_source_run_id,
                record.manifest.resume_source_run_key_name,
            ),
            history,
        )
    return record


def resolve_checkpoint_source(
    record: RunHistoryRecord, node_id: str, history: RunHistoryScan
) -> RunHistoryRecord | None:
    checkpoint = next(
        (
            item
            for item in record.manifest.resumed_review_checkpoints
            if item.node_id == node_id
        ),
        None,
    )
    if checkpoint is None:
        return None
    origin = checkpoint.resume_origin
    if origin.source_node_id != node_id:
        raise RunHistorySourceError(f"Checkpoint source node differs from {node_id}.")
    source = _source_record(
        record, (origin.source_run_id, origin.source_run_key_name), history
    )
    if source.manifest.run_key_name == record.manifest.run_key_name:
        raise RunHistorySourceError(f"Cyclic checkpoint source for node {node_id}.")
    return source


def _source_record(
    referencing: RunHistoryRecord,
    identity: tuple[str | None, str | None],
    history: RunHistoryScan,
) -> RunHistoryRecord:
    run_id, key = identity
    source = next(
        (record for record in history.records if record.manifest.run_key_name == key),
        None,
    )
    if source is None:
        diagnostic = next(
            (item for item in history.diagnostics if item.run_key_name == key), None
        )
        reason = diagnostic.message if diagnostic else "No saved execution manifest."
        raise RunHistorySourceError(f"Source run {key} unavailable: {reason}")
    expected = referencing.manifest
    actual = source.manifest
    if actual.run_id != run_id:
        raise RunHistorySourceError(f"Source run {key} has a mismatched run ID.")
    if (
        actual.workflow_identity != expected.workflow_identity
        or actual.workflow_name != expected.workflow_name
        or actual.workflow_signature != expected.workflow_signature
    ):
        raise RunHistorySourceError(
            f"Source run {key} has a mismatched workflow context."
        )
    return source
