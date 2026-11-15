from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from crewplane.artifacts.run_history import scan_run_history
from crewplane.cli.runs.logs import (
    SavedLogSelectionError,
    latest_invocation_log,
    select_saved_log,
    validate_log_reference,
)
from crewplane.core.execution_state import ResumeOrigin, ReviewCheckpointResumeSummary
from crewplane.observability.events import EventType
from tests.helpers.platforms import symlink_or_skip
from tests.helpers.resume import write_run_manifest
from tests.helpers.saved_runs import saved_event, saved_run, write_events


def invocation(
    record, task: str, timestamp: str, kind=EventType.INVOCATION_STARTED, **fields
):
    return saved_event(
        record,
        kind,
        node_id="import.review[2]",
        task_id=task,
        provider="mock",
        round_num=1,
        timestamp_utc=timestamp,
        log_file=f"node/{task}.log",
        **fields,
    )


def test_timestamp_order_ties_and_lifecycle_dedup(tmp_path: Path) -> None:
    record = saved_run(tmp_path)
    events = (
        invocation(record, "newer", "2026-01-01T00:00:03+00:00"),
        invocation(record, "older", "2026-01-01T00:00:01+00:00"),
        invocation(
            record, "older", "2026-01-01T00:00:09+00:00", EventType.INVOCATION_FINISHED
        ),
    )
    selected = latest_invocation_log(events, "import.review[2]")
    assert selected is not None and selected.task_id == "newer"
    tied = (*events, invocation(record, "tied", "2026-01-01T00:00:03+00:00"))
    selected = latest_invocation_log(tied, "import.review[2]")
    assert selected is not None and selected.task_id == "tied"
    assert latest_invocation_log(tied, "review") is None


def test_started_without_reference_uses_terminal_reference_but_not_finish_time(
    tmp_path: Path,
) -> None:
    record = saved_run(tmp_path)
    events = (
        saved_event(
            record,
            EventType.INVOCATION_STARTED,
            node_id="n",
            task_id="a",
            provider="p",
            round_num=0,
            timestamp_utc="2026-01-01T00:00:01Z",
        ),
        saved_event(
            record,
            EventType.INVOCATION_STARTED,
            node_id="n",
            task_id="b",
            provider="p",
            round_num=0,
            timestamp_utc="2026-01-01T00:00:02Z",
            log_file="b.log",
        ),
        saved_event(
            record,
            EventType.INVOCATION_FINISHED,
            node_id="n",
            task_id="a",
            provider="p",
            round_num=0,
            timestamp_utc="2026-01-01T00:00:09Z",
            log_file="a.log",
        ),
    )
    selected = latest_invocation_log(events, "n")
    assert selected is not None and selected.task_id == "b"


@pytest.mark.parametrize("terminal_after", [True, False])
def test_mixed_terminal_only_fallback_uses_order_not_finish_timestamp(
    tmp_path: Path, terminal_after: bool
) -> None:
    record = saved_run(tmp_path)
    start = invocation(record, "started", "2026-01-01T00:00:05Z")
    terminal = invocation(
        record, "terminal", "2026-01-01T00:00:01Z", EventType.INVOCATION_FINISHED
    )
    events = (start, terminal) if terminal_after else (terminal, start)
    selected = latest_invocation_log(events, "import.review[2]")
    assert selected is not None
    assert selected.task_id == ("terminal" if terminal_after else "started")
    if terminal_after:
        assert selected.started_at is None


def test_terminal_only_and_repeated_start_references(tmp_path: Path) -> None:
    record = saved_run(tmp_path)
    events = (
        invocation(
            record, "one", "2026-01-01T00:00:03Z", EventType.INVOCATION_FINISHED
        ),
        invocation(record, "two", "2026-01-01T00:00:01Z", EventType.INVOCATION_FAILED),
    )
    assert latest_invocation_log(events, "import.review[2]").task_id == "two"
    starts = (
        invocation(record, "one", "2026-01-01T00:00:01Z"),
        invocation(record, "two", "2026-01-01T00:00:02Z"),
        invocation(record, "one", "2026-01-01T00:00:03Z"),
        invocation(record, "one", "2026-01-01T00:00:00Z"),
    )
    assert latest_invocation_log(starts, "import.review[2]").task_id == "one"


def test_provider_audit_round_coordinates_are_distinct(tmp_path: Path) -> None:
    record = saved_run(tmp_path)
    events = tuple(
        saved_event(
            record,
            EventType.INVOCATION_STARTED,
            node_id="n",
            task_id="shared",
            provider=provider,
            audit_round_num=audit,
            round_num=round_num,
            log_file=f"{index}.log",
            timestamp_utc=f"2026-01-01T00:00:0{index}Z",
        )
        for index, (provider, audit, round_num) in enumerate(
            [("p", 1, 0), ("q", 1, 0), ("q", 2, 0), ("q", 2, 1)]
        )
    )
    selected = latest_invocation_log(events, "n")
    assert selected is not None
    assert (selected.provider, selected.audit_round_num, selected.round_num) == (
        "q",
        2,
        1,
    )


def test_conflicting_lifecycle_log_references_fail(tmp_path: Path) -> None:
    record = saved_run(tmp_path)
    events = tuple(
        saved_event(
            record,
            kind,
            node_id="n",
            task_id="t",
            provider="p",
            round_num=1,
            log_file=path,
        )
        for kind, path in [
            (EventType.INVOCATION_STARTED, "a"),
            (EventType.INVOCATION_FINISHED, "b"),
        ]
    )
    with pytest.raises(SavedLogSelectionError, match="Conflicting"):
        latest_invocation_log(events, "n")


@pytest.mark.parametrize("coordinate", ["task_id", "provider", "round_num"])
def test_incomplete_invocation_coordinates_fail_without_a_log_reference(
    tmp_path: Path, coordinate: str
) -> None:
    record = saved_run(tmp_path)
    event = saved_event(
        record,
        EventType.INVOCATION_STARTED,
        node_id="n",
        task_id="t",
        provider="p",
        round_num=1,
    )
    event = replace(event, context=replace(event.context, **{coordinate: None}))
    with pytest.raises(SavedLogSelectionError) as error:
        latest_invocation_log((event,), "n")
    assert str(error.value) == "Incomplete invocation coordinates; retry inspection."


def test_start_order_survives_older_repeated_start_and_later_finish(
    tmp_path: Path,
) -> None:
    record = saved_run(tmp_path)
    events = (
        invocation(record, "one", "2026-01-01T00:00:05Z"),
        invocation(record, "one", "2026-01-01T00:00:01Z"),
        invocation(
            record, "one", "2026-01-01T00:00:09Z", EventType.INVOCATION_FINISHED
        ),
    )
    selected = latest_invocation_log(events, "import.review[2]")
    assert selected is not None
    assert selected.started_at == events[0].timestamp
    assert selected.event_order == 0
    assert selected.log_file == "node/one.log"


@pytest.mark.parametrize("kind", ["relative", "absolute"])
def test_exact_contained_log_paths(tmp_path: Path, kind: str) -> None:
    record = saved_run(tmp_path)
    path = record.run_dir / "nested/provider.log"
    path.parent.mkdir()
    path.write_bytes(b"raw")
    reference = str(path) if kind == "absolute" else "nested/provider.log"
    assert validate_log_reference(record, reference) == path


@pytest.mark.parametrize(
    "reference",
    [
        "../outside.log",
        "nested/../provider.log",
        "/outside/provider.log",
        r"nested\provider.log",
        "http://example.com/log",
        "",
        "./provider.log",
    ],
)
def test_unsafe_references_are_not_rebased(tmp_path: Path, reference: str) -> None:
    record = saved_run(tmp_path)
    with pytest.raises(SavedLogSelectionError):
        validate_log_reference(record, reference)


@pytest.mark.parametrize(
    "reference,message,cause",
    [
        (
            "../missing.log",
            "Saved log reference contains unsafe traversal or separators.",
            None,
        ),
        (
            "/outside/provider.log",
            "Saved log reference is outside its evidence run: /outside/provider.log. "
            "Archived absolute paths are not rebased.",
            "outside",
        ),
        (
            "./missing.log",
            "Saved log reference is not a safe contained path: ./missing.log",
            None,
        ),
        (
            "missing.log",
            "Saved event references a missing provider log: {run_dir}/missing.log",
            None,
        ),
        (
            "directory",
            "Cannot safely read saved provider log: {run_dir}/directory",
            "not a single-link regular file",
        ),
    ],
)
def test_reference_failure_messages_and_causes(
    tmp_path: Path, reference: str, message: str, cause: str | None
) -> None:
    record = saved_run(tmp_path)
    (record.run_dir / "directory").mkdir()
    with pytest.raises(SavedLogSelectionError) as error:
        validate_log_reference(record, reference)
    assert str(error.value) == message.format(run_dir=record.run_dir)
    if cause is None:
        assert error.value.__cause__ is None
    else:
        assert isinstance(error.value.__cause__, ValueError)
        assert cause in str(error.value.__cause__)


@pytest.mark.parametrize(
    "kind", ["symlink", "hardlink", "directory", "parent_symlink", "dangling"]
)
def test_unsafe_file_aliases_rejected(tmp_path: Path, kind: str) -> None:
    record = saved_run(tmp_path)
    target = record.run_dir / "log"
    original = tmp_path / "original"
    original.write_bytes(b"not accessible by alias")
    if kind == "symlink":
        symlink_or_skip(target, original)
    elif kind == "hardlink":
        target.hardlink_to(original)
    elif kind == "directory":
        target.mkdir()
    elif kind == "dangling":
        symlink_or_skip(target, tmp_path / "missing")
    else:
        symlink_or_skip(target, tmp_path, target_is_directory=True)
    reference = "log/original" if kind == "parent_symlink" else "log"
    with pytest.raises(SavedLogSelectionError, match="safely"):
        validate_log_reference(record, reference)


def checkpoint(record, source, node: str = "n") -> None:
    summary = ReviewCheckpointResumeSummary(
        node_id=node,
        source_audit=1,
        source_local_round=1,
        source_phase="reviewers",
        resume_origin=ResumeOrigin(
            source_run_id=source.manifest.run_id,
            source_run_key_name=source.manifest.run_key_name,
            source_node_id=node,
            hydrated_at=record.manifest.started_at,
        ),
    )
    write_run_manifest(
        record.run_dir.parent.parent,
        record.manifest.model_copy(
            update={
                "resumed_review_checkpoints": [summary],
                "resume_source_run_id": source.manifest.run_id,
                "resume_source_run_key_name": source.manifest.run_key_name,
            }
        ),
    )


def node_log(record, node: str = "n", filename: str = "provider.log") -> Path:
    path = record.run_dir / filename
    path.write_bytes(filename.encode())
    write_events(
        record,
        [
            saved_event(
                record,
                EventType.INVOCATION_STARTED,
                node_id=node,
                task_id="t",
                provider="p",
                round_num=1,
                log_file=filename,
            )
        ],
    )
    return path


@pytest.mark.parametrize("current_evidence", [True, False])
def test_checkpoint_prefers_current_and_falls_back_only_without_evidence(
    tmp_path: Path, current_evidence: bool
) -> None:
    source = saved_run(tmp_path, "old", "workflow--old")
    source_path = node_log(source)
    current = saved_run(tmp_path, "new", "workflow--new", started_offset=1)
    checkpoint(current, source)
    current_path = node_log(current) if current_evidence else None
    if not current_evidence:
        write_events(
            current, [saved_event(current, EventType.NODE_STARTED, node_id="n")]
        )
    history = scan_run_history(tmp_path)
    selected = select_saved_log(history.records[0], "n", history)
    assert selected.path == (current_path if current_evidence else source_path)
    assert selected.source.manifest.run_id == ("new" if current_evidence else "old")


def test_completed_node_source_and_stopping_at_rerun(tmp_path: Path) -> None:
    source = saved_run(tmp_path, "old", "workflow--old")
    node_log(source)
    middle = saved_run(tmp_path, "mid", "workflow--mid", started_offset=1)
    rerun_path = node_log(middle)
    current = saved_run(
        tmp_path,
        "new",
        "workflow--new",
        started_offset=2,
        resumed_nodes=["n"],
        resume_source_run_id="mid",
        resume_source_run_key_name="workflow--mid",
    )
    write_events(current, [saved_event(current)])
    history = scan_run_history(tmp_path)
    selected = select_saved_log(history.records[0], "n", history)
    assert selected.source.manifest.run_id == "mid"
    assert selected.path == rerun_path


@pytest.mark.parametrize(
    "damage", ["partial", "corrupt", "missing", "mismatch", "timestamp", "coordinates"]
)
def test_bad_current_checkpoint_evidence_never_falls_back(
    tmp_path: Path, damage: str
) -> None:
    source = saved_run(tmp_path, "old", "workflow--old")
    node_log(source)
    current = saved_run(tmp_path, "new", "workflow--new", started_offset=1)
    checkpoint(current, source)
    if damage != "missing":
        path = write_events(current, [saved_event(current)])
        if damage == "partial":
            path.write_bytes(path.read_bytes() + b"{")
        elif damage == "corrupt":
            path.write_bytes(path.read_bytes() + b"bad\n")
        elif damage == "mismatch":
            write_events(current, [saved_event(current, run_id="other")])
        elif damage == "timestamp":
            write_events(current, [saved_event(current, timestamp_utc="nonsense")])
        else:
            node_log(current)
            event_path = current.run_dir / "logs/events.ndjson"
            payload = json.loads(event_path.read_bytes())
            del payload["task_id"]
            event_path.write_bytes((json.dumps(payload) + "\n").encode())
    history = scan_run_history(tmp_path)
    with pytest.raises(SavedLogSelectionError, match="evidence incomplete"):
        select_saved_log(history.records[0], "n", history)


def test_checkpoint_chain_cycle_and_missing_source(tmp_path: Path) -> None:
    source = saved_run(tmp_path, "old", "workflow--old")
    current = saved_run(tmp_path, "new", "workflow--new", started_offset=1)
    for record in (source, current):
        write_events(record, [saved_event(record)])
    checkpoint(current, source)
    checkpoint(source, current)
    history = scan_run_history(tmp_path)
    with pytest.raises(SavedLogSelectionError, match="Cyclic"):
        select_saved_log(history.records[0], "n", history)
    source.manifest_path.unlink()
    history = scan_run_history(tmp_path)
    with pytest.raises(ValueError, match="unavailable"):
        select_saved_log(history.records[0], "n", history)


def test_no_evidence_differs_from_missing_referenced_file(tmp_path: Path) -> None:
    record = saved_run(tmp_path)
    write_events(
        record, [saved_event(record, EventType.NODE_FINISHED, node_id="input")]
    )
    history = scan_run_history(tmp_path)
    with pytest.raises(SavedLogSelectionError, match="Observed node IDs: input"):
        select_saved_log(record, "input", history)
    path = node_log(record)
    path.unlink()
    with pytest.raises(
        SavedLogSelectionError, match="references a missing provider log"
    ):
        select_saved_log(record, "n", history)
    with pytest.raises(SavedLogSelectionError, match="nonempty"):
        select_saved_log(record, "", history)


@pytest.mark.parametrize(
    "nodes,suffix",
    [
        ((), " No node IDs were recorded."),
        (("z", "a", "z"), " Observed node IDs: a, z."),
    ],
)
def test_missing_evidence_diagnostic_lists_sorted_unique_nodes(
    tmp_path: Path, nodes: tuple[str, ...], suffix: str
) -> None:
    record = saved_run(tmp_path)
    write_events(
        record,
        [saved_event(record, EventType.NODE_FINISHED, node_id=node) for node in nodes],
    )
    with pytest.raises(SavedLogSelectionError) as error:
        select_saved_log(record, "missing", scan_run_history(tmp_path))
    assert str(error.value) == (
        "No saved provider log evidence for node 'missing' in workflow--run-1. "
        "The node may be unknown, pending, input-only, or have no captured output."
        + suffix
    )


def test_incomplete_evidence_diagnostic_preserves_unique_reason_order(
    tmp_path: Path,
) -> None:
    record = saved_run(tmp_path)
    path = write_events(record, [])
    path.write_bytes(b"bad\nbad\n\xff\n{")
    with pytest.raises(SavedLogSelectionError) as error:
        select_saved_log(record, "n", scan_run_history(tmp_path))
    assert str(error.value) == (
        "Cannot safely choose the latest invocation for node 'n': "
        "evidence incomplete in workflow--run-1 (Malformed saved event record., "
        "Invalid UTF-8 in saved event record., "
        "Unfinished trailing event append; retry inspection.). "
        "Retry when the saved event log is complete, or inspect it with "
        "crewplane runs logs workflow--run-1 without --node."
    )
