from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from crewplane.artifacts.run_history import RunHistoryRecord, scan_run_history
from crewplane.cli.app import app
from crewplane.cli.runs.listing import print_saved_runs, run_attention, saved_run_rows
from crewplane.core.execution_state import ResumeOrigin, ReviewCheckpointResumeSummary
from crewplane.observability.events import EventType, ExecutionEvent
from crewplane.observability.events.inspection import EventLogInspection
from tests.helpers.resume import iso_datetime, write_run_manifest
from tests.helpers.saved_runs import saved_event, saved_run, write_events


def test_empty_catalogue_succeeds_without_creating_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["runs", "list"])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "No saved execution runs found."
    assert not (tmp_path / ".crewplane").exists()


@pytest.mark.parametrize("status", ["running", "succeeded", "failed", "cancelled"])
def test_recorded_outcome_elapsed_and_attention(tmp_path: Path, status: str) -> None:
    record = saved_run(tmp_path, status=status)
    write_events(record, [saved_event(record)])
    row = saved_run_rows(scan_run_history(tmp_path))[0]
    assert row.outcome == ("running (recorded)" if status == "running" else status)
    assert row.elapsed == ("n/a" if status == "running" else "1.0s")
    assert row.reused == "no"
    assert row.started.endswith("(recorded local time)")
    expected = {
        "running": "evidence incomplete",
        "failed": "failed",
        "cancelled": "cancelled",
        "succeeded": "none recorded",
    }
    assert row.attention == expected[status]


@pytest.mark.parametrize(
    "end,expected",
    [
        ("2026-06-09T12:00:00", "0.0s"),
        ("2026-06-09T11:59:59", "n/a"),
        ("2026-06-09T12:00:10+00:00", "n/a"),
    ],
)
def test_inconsistent_and_zero_manifest_elapsed(
    tmp_path: Path, end: str, expected: str
) -> None:
    saved_run(tmp_path, completed_at=end)
    assert saved_run_rows(scan_run_history(tmp_path))[0].elapsed == expected


def test_offset_timestamp_is_not_labeled_local(tmp_path: Path) -> None:
    saved_run(
        tmp_path,
        started_at="2026-06-09T12:00:00+02:00",
        completed_at="2026-06-09T10:00:01+00:00",
    )
    row = saved_run_rows(scan_run_history(tmp_path))[0]
    assert row.started == "2026-06-09T12:00:00+02:00"
    assert row.elapsed == "1.0s"


@pytest.mark.parametrize(
    "operation,attributes",
    [
        ("review_loop_consensus_exhausted", {"continued": True}),
        ("review_loop_stopped", {"continued": True, "stop_reason": "no_progress"}),
    ],
)
@pytest.mark.parametrize("corrupt_attributes", [False, True])
def test_unresolved_review_projection(
    tmp_path: Path,
    operation: str,
    attributes: dict[str, object],
    corrupt_attributes: bool,
) -> None:
    record = saved_run(tmp_path)
    path = write_events(
        record,
        [
            saved_event(
                record,
                EventType.RUNTIME_LOG,
                node_id="review",
                level="warning",
                message="not resolved",
                operation=operation,
                attributes=attributes,
            ),
            saved_event(record),
        ],
    )
    if corrupt_attributes:
        events = [json.loads(line) for line in path.read_bytes().splitlines()]
        events[0]["attributes"]["bad"] = []
        path.write_bytes(
            "".join(json.dumps(event) + "\n" for event in events).encode("utf-8")
        )
    expected = "evidence incomplete" if corrupt_attributes else "review unresolved"
    assert saved_run_rows(scan_run_history(tmp_path))[0].attention == expected


@pytest.mark.parametrize(
    "level,expected", [("warning", "none recorded"), ("error", "errors recorded")]
)
def test_saved_issues_attention(tmp_path: Path, level: str, expected: str) -> None:
    record = saved_run(tmp_path)
    write_events(
        record,
        [
            saved_event(
                record,
                EventType.RUNTIME_LOG,
                level=level,
                message="saved problem",
                operation="some_issue",
            ),
            saved_event(record),
        ],
    )
    assert saved_run_rows(scan_run_history(tmp_path))[0].attention == expected


@pytest.mark.parametrize(
    "damage", ["missing", "partial", "corrupt", "mismatched", "unfinished"]
)
def test_evidence_unknown_is_not_clean(tmp_path: Path, damage: str) -> None:
    record = saved_run(tmp_path, status="failed")
    if damage != "missing":
        events = [saved_event(record)] if damage != "unfinished" else []
        path = write_events(record, events)
        if damage == "partial":
            path.write_bytes(path.read_bytes() + b'{"event_type":')
        elif damage == "corrupt":
            path.write_bytes(path.read_bytes() + b"broken\n")
        elif damage == "mismatched":
            write_events(record, [saved_event(record, run_id="other")])
    assert (
        saved_run_rows(scan_run_history(tmp_path))[0].attention
        == "failed, evidence incomplete"
    )


def _review_events(
    record: RunHistoryRecord, unresolved_node: str
) -> list[ExecutionEvent]:
    return [
        saved_event(
            record,
            EventType.RUNTIME_LOG,
            node_id=unresolved_node,
            level="warning",
            message="review",
            operation="review_loop_consensus_exhausted",
            attributes={"continued": True},
        ),
        saved_event(record, EventType.NODE_FINISHED, node_id="kept"),
        saved_event(record, EventType.NODE_FINISHED, node_id="rerun"),
        saved_event(record),
    ]


@pytest.mark.parametrize(
    "unresolved_node,expected",
    [("kept", "review unresolved"), ("rerun", "none recorded")],
)
def test_reused_review_is_node_scoped_and_repeated(
    tmp_path: Path, unresolved_node: str, expected: str
) -> None:
    origin = saved_run(tmp_path, "old", "workflow--old", status="failed")
    write_events(origin, _review_events(origin, unresolved_node))
    middle = saved_run(
        tmp_path,
        "middle",
        "workflow--middle",
        started_offset=1,
        resumed_nodes=["kept"],
        resume_source_run_id="old",
        resume_source_run_key_name="workflow--old",
    )
    write_events(middle, [saved_event(middle)])
    newest = saved_run(
        tmp_path,
        "new",
        "workflow--new",
        started_offset=2,
        resumed_nodes=["kept"],
        resume_source_run_id="middle",
        resume_source_run_key_name="workflow--middle",
    )
    write_events(newest, [saved_event(newest)])
    row = saved_run_rows(scan_run_history(tmp_path))[0]
    assert row.attention == expected
    assert row.reused == "yes (1 nodes, 0 checkpoints)"


def test_attention_shares_event_snapshots_across_reused_nodes_and_rows(
    tmp_path: Path,
) -> None:
    source = saved_run(tmp_path, "old", "workflow--old")
    source_log = write_events(source, _review_events(source, "kept"))
    current = saved_run(
        tmp_path,
        "new",
        "workflow--new",
        started_offset=1,
        resumed_nodes=["kept", "rerun"],
        resume_source_run_id="old",
        resume_source_run_key_name="workflow--old",
    )
    write_events(current, [saved_event(current)])
    history = scan_run_history(tmp_path)
    evidence: dict[str, EventLogInspection] = {}

    assert run_attention(current, history, evidence) == "review unresolved"
    assert set(evidence) == {"workflow--old", "workflow--new"}
    source_log.unlink()
    assert run_attention(source, history, evidence) == "review unresolved"
    assert run_attention(current, history, evidence) == "review unresolved"


@pytest.mark.parametrize("damage", ["missing", "mismatch", "cycle", "partial"])
def test_reused_missing_or_unsafe_provenance_is_incomplete(
    tmp_path: Path, damage: str
) -> None:
    source = saved_run(tmp_path, "old", "workflow--old")
    write_events(source, _review_events(source, "kept"))
    newest = saved_run(
        tmp_path,
        "new",
        "workflow--new",
        started_offset=2,
        resumed_nodes=["kept"],
        resume_source_run_id="old",
        resume_source_run_key_name="workflow--old",
    )
    write_events(newest, [saved_event(newest)])
    if damage == "missing":
        source.manifest_path.unlink()
    elif damage == "mismatch":
        write_run_manifest(
            tmp_path, source.manifest.model_copy(update={"workflow_name": "Other"})
        )
    elif damage == "cycle":
        write_run_manifest(
            tmp_path,
            source.manifest.model_copy(
                update={
                    "resumed_nodes": ["kept"],
                    "resume_source_run_id": "new",
                    "resume_source_run_key_name": "workflow--new",
                }
            ),
        )
    else:
        path = source.run_dir / "logs/events.ndjson"
        path.write_bytes(path.read_bytes() + b"{")
    attention = saved_run_rows(scan_run_history(tmp_path))[0].attention
    assert "evidence incomplete" in attention
    if damage == "partial":
        assert "review unresolved" in attention


@pytest.mark.parametrize("width", [20, 40, 60])
def test_duplicate_identifiers_and_narrow_literal_table(
    tmp_path: Path, width: int
) -> None:
    key = "workflow-with-a-very-long-key--20260609-120000"
    first = saved_run(tmp_path, "same", key, workflow_name="[red]長い workflow[/red]")
    second = saved_run(
        tmp_path, "same", "second-long-workflow--same", workflow_name="Other"
    )
    for record in (first, second):
        write_events(record, [saved_event(record)])
    history = scan_run_history(tmp_path)
    assert {row.selector for row in saved_run_rows(history)} == {
        key,
        second.manifest.run_key_name,
    }
    stream = io.StringIO()
    print_saved_runs(
        history,
        Console(file=stream, width=width, force_terminal=False, color_system=None),
    )
    output = stream.getvalue()
    assert "..." not in output and "…" not in output
    reconstructed_rows: list[list[str]] = []
    fragments: list[list[str]] = [[] for _ in range(7)]
    for line in output.splitlines():
        cells = line.split("|")
        if len(cells) == 9:
            assert not cells[0].strip() and not cells[-1].strip()
            for column, cell in zip(fragments, cells[1:-1], strict=True):
                column.append(cell.strip())
        elif any(fragments):
            reconstructed_rows.append(
                ["".join(column).replace(" ", "") for column in fragments]
            )
            fragments = [[] for _ in range(7)]
    assert reconstructed_rows[0] == [
        "Workflow",
        "Run",
        "Started",
        "Outcome",
        "Elapsed",
        "Reused",
        "Attention",
    ]
    expected = [
        [
            row.workflow,
            row.selector,
            row.started,
            row.outcome,
            row.elapsed,
            row.reused,
            row.attention,
        ]
        for row in saved_run_rows(history)
    ]
    assert reconstructed_rows[1:] == [
        [cell.replace(" ", "") for cell in row] for row in expected
    ]


def test_ascii_output_escapes_unicode_without_failure(tmp_path: Path) -> None:
    record = saved_run(tmp_path, workflow_name="café 漢字")
    write_events(record, [saved_event(record)])
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="ascii")
    print_saved_runs(
        scan_run_history(tmp_path),
        Console(file=stream, width=160, force_terminal=False, color_system=None),
    )
    stream.flush()
    assert b"caf\\xe9" in raw.getvalue()


def _checkpoint(source: RunHistoryRecord) -> ReviewCheckpointResumeSummary:
    return ReviewCheckpointResumeSummary(
        node_id="review",
        source_audit=1,
        source_local_round=0,
        source_phase="reviewers",
        resume_origin=ResumeOrigin(
            source_run_id=source.manifest.run_id,
            source_run_key_name=source.manifest.run_key_name,
            source_node_id="review",
            hydrated_at=iso_datetime(),
        ),
    )


@pytest.mark.parametrize("reuse_completed_node", [False, True])
def test_checkpoint_current_review_is_independent_of_old_unresolved_review(
    tmp_path: Path, reuse_completed_node: bool
) -> None:
    source = saved_run(tmp_path, "old", "workflow--old", status="failed")
    write_events(source, _review_events(source, "review"))
    current = saved_run(
        tmp_path,
        "new",
        "workflow--new",
        started_offset=2,
        resumed_nodes=["kept"] if reuse_completed_node else [],
        resumed_review_checkpoints=[_checkpoint(source)],
        resume_source_run_id=source.manifest.run_id,
        resume_source_run_key_name=source.manifest.run_key_name,
    )
    write_events(
        current,
        [
            saved_event(current, EventType.NODE_STARTED, node_id="review"),
            saved_event(current, EventType.NODE_FINISHED, node_id="review"),
            saved_event(current),
        ],
    )
    rows = saved_run_rows(scan_run_history(tmp_path))
    assert rows[0].attention == "none recorded"
    assert rows[0].reused == f"yes ({int(reuse_completed_node)} nodes, 1 checkpoints)"
    assert rows[1].attention == "failed, review unresolved"


@pytest.mark.parametrize("node_started", [False, True])
def test_checkpoint_unfinished_current_node_is_incomplete_despite_terminal_workflow(
    tmp_path: Path, node_started: bool
) -> None:
    source = saved_run(tmp_path, "old", "workflow--old")
    write_events(source, _review_events(source, "review"))
    current = saved_run(
        tmp_path,
        "new",
        "workflow--new",
        started_offset=2,
        resumed_review_checkpoints=[_checkpoint(source)],
        resume_source_run_id=source.manifest.run_id,
        resume_source_run_key_name=source.manifest.run_key_name,
    )
    events = (
        [saved_event(current, EventType.NODE_STARTED, node_id="review")]
        if node_started
        else []
    )
    write_events(current, [*events, saved_event(current)])
    row = saved_run_rows(scan_run_history(tmp_path))[0]
    assert row.outcome == "succeeded"
    assert row.attention == "evidence incomplete"


@pytest.mark.parametrize("damage", ["missing", "partial"])
def test_checkpoint_incomplete_evidence_preserves_known_current_flags(
    tmp_path: Path, damage: str
) -> None:
    source = saved_run(tmp_path, "old", "workflow--old")
    write_events(source, [saved_event(source)])
    current = saved_run(
        tmp_path,
        "new",
        "workflow--new",
        status="failed",
        started_offset=2,
        resumed_review_checkpoints=[_checkpoint(source)],
        resume_source_run_id=source.manifest.run_id,
        resume_source_run_key_name=source.manifest.run_key_name,
    )
    if damage == "partial":
        path = write_events(
            current,
            [
                saved_event(
                    current,
                    EventType.RUNTIME_LOG,
                    node_id="review",
                    level="error",
                    message="current unresolved review",
                    operation="review_loop_consensus_exhausted",
                    attributes={"continued": True},
                ),
                saved_event(current, EventType.NODE_FINISHED, node_id="review"),
                saved_event(current),
            ],
        )
        path.write_bytes(path.read_bytes() + b'{"event_type":')
    row = saved_run_rows(scan_run_history(tmp_path))[0]
    assert row.attention == (
        "failed, errors recorded, review unresolved, evidence incomplete"
        if damage == "partial"
        else "failed, evidence incomplete"
    )


def test_unnormalizable_completed_time_is_unavailable_elapsed(tmp_path: Path) -> None:
    saved_run(
        tmp_path,
        started_at="2026-06-09T12:00:00+00:00",
        completed_at="9999-12-31T23:59:59-23:59",
    )
    assert saved_run_rows(scan_run_history(tmp_path))[0].elapsed == "n/a"


def test_preflight_only_attempt_count_is_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    attempt = tmp_path / ".crewplane/execution-stages/preflight-attempt"
    attempt.mkdir(parents=True)
    result = CliRunner().invoke(app, ["runs", "list"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "No saved execution runs found."
    assert "Excluded 1 directory(s) without execution manifests" in result.stderr
    assert "preflight or incomplete attempts" in result.stderr
