from __future__ import annotations

import pytest

from crewplane.artifacts.run_history import scan_run_history
from crewplane.artifacts.run_history_sources import (
    RunHistorySourceError,
    resolve_checkpoint_source,
    resolve_completed_node_source,
)
from crewplane.core.execution_state import ResumeOrigin, ReviewCheckpointResumeSummary
from tests.helpers.resume import iso_datetime, make_run_manifest, write_run_manifest


def _resumed(key, source, nodes=("node",)):
    manifest = make_run_manifest(key, key)
    manifest.resumed_nodes = list(nodes)
    manifest.resume_source_run_id = source
    manifest.resume_source_run_key_name = source
    return manifest


def test_completed_node_walk_is_exact_and_stops_at_rerun(tmp_path) -> None:
    for manifest in [
        make_run_manifest("origin", "origin"),
        _resumed("middle", "origin"),
        _resumed("latest", "middle", ("node", "rerun")),
    ]:
        write_run_manifest(tmp_path, manifest)
    history = scan_run_history(tmp_path)
    latest = next(item for item in history.records if item.manifest.run_id == "latest")
    assert (
        resolve_completed_node_source(latest, "node", history).manifest.run_id
        == "origin"
    )
    assert (
        resolve_completed_node_source(latest, "rerun", history).manifest.run_id
        == "middle"
    )
    assert resolve_completed_node_source(latest, "other", history) is latest


@pytest.mark.parametrize("failure", ["missing", "excluded", "id", "context", "cycle"])
def test_completed_source_failures_are_explicit(tmp_path, failure) -> None:
    current = _resumed("current", "source")
    source = make_run_manifest("source", "source")
    if failure == "id":
        source.run_id = "different"
    elif failure == "context":
        source.workflow_name = "Different"
    elif failure == "cycle":
        source = _resumed("source", "current")
    if failure != "missing":
        path = write_run_manifest(tmp_path, source)
        if failure == "excluded":
            path.write_bytes(b"{")
    write_run_manifest(tmp_path, current)
    history = scan_run_history(tmp_path)
    record = next(item for item in history.records if item.manifest.run_id == "current")
    with pytest.raises(RunHistorySourceError):
        resolve_completed_node_source(record, "node", history)


def test_checkpoint_uses_explicit_origin_and_validates_context(tmp_path) -> None:
    source = make_run_manifest("source", "source")
    current = _resumed("current", "source", ())
    current.resumed_review_checkpoints = [
        ReviewCheckpointResumeSummary(
            node_id="review",
            source_audit=1,
            source_local_round=0,
            source_phase="reviewers",
            resume_origin=ResumeOrigin(
                source_run_id="source",
                source_run_key_name="source",
                source_node_id="review",
                hydrated_at=iso_datetime(),
            ),
        )
    ]
    write_run_manifest(tmp_path, source)
    write_run_manifest(tmp_path, current)
    history = scan_run_history(tmp_path)
    record = next(item for item in history.records if item.manifest.run_id == "current")
    resolved = resolve_checkpoint_source(record, "review", history)
    assert resolved is not None and resolved.manifest.run_id == "source"
    assert resolve_checkpoint_source(record, "other", history) is None
    source.workflow_name = "Other"
    write_run_manifest(tmp_path, source)
    with pytest.raises(RunHistorySourceError, match="workflow context"):
        resolve_checkpoint_source(record, "review", scan_run_history(tmp_path))
