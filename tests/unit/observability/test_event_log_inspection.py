from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.core.workflow.keywords import ProviderRole
from crewplane.observability.events import EventType, execution_event_log_record
from crewplane.observability.events.inspection import (
    EventLogDiagnostic,
    EventLogInspection,
)
from crewplane.observability.events.reader import inspect_event_log, read_event_log
from tests.helpers.observability import make_execution_event
from tests.helpers.platforms import symlink_or_skip


def event_bytes(**changes: object) -> bytes:
    event = make_execution_event(
        EventType.INVOCATION_STARTED,
        workflow_name="workflow",
        run_id="run-1",
        node_id="import.node[0]",
        task_id="task",
        provider="mock",
        role=ProviderRole.EXECUTOR,
        round_num=0,
        timestamp=0.0,
        timestamp_utc="2026-08-04T00:00:00+00:00",
    )
    record: dict[str, object] = dict(execution_event_log_record(event))
    record.update(changes)
    return (json.dumps(record) + "\n").encode("utf-8")


def inspect(path: Path) -> EventLogInspection:
    return inspect_event_log(path, "workflow", "run-1")


def test_valid_blank_and_crlf_records_preserve_order(tmp_path: Path) -> None:
    path = tmp_path / "events.ndjson"
    path.write_bytes(
        b"\n \r\n"
        + event_bytes(task_id="first")
        + event_bytes(task_id="second").replace(b"\n", b"\r\n")
    )
    result = inspect(path)
    assert result.complete and result.ordering_reliable and not result.partial
    assert [event.context.task_id for event in result.events] == ["first", "second"]


@pytest.mark.parametrize(
    "bad,category",
    [
        (b"not json\n", "malformed"),
        (b"[]\n", "malformed"),
        (b'{"number":' + b"9" * 5000 + b"}\n", "malformed"),
        (b"\xff\n", "encoding"),
        (event_bytes(workflow_name="other"), "identity"),
        (event_bytes(run_id="other"), "identity"),
        (event_bytes(timestamp="invalid"), "invalid"),
        (event_bytes(node_id=" "), "invalid"),
        (event_bytes(task_id=None), "invalid"),
        (event_bytes(provider=""), "invalid"),
        (event_bytes(round_num=-1), "invalid"),
        (event_bytes(round_num=True), "invalid"),
        (event_bytes(round_num=None), "invalid"),
        (event_bytes(audit_round_num=0), "invalid"),
        (event_bytes(audit_round_num="1"), "invalid"),
        (event_bytes(audit_round_num=True), "invalid"),
        (event_bytes(log_file={"path": "new.log"}), "invalid"),
        (event_bytes(log_file=42), "invalid"),
        (event_bytes(log_file=""), "invalid"),
        (event_bytes(log_file="   "), "invalid"),
    ],
)
def test_bad_complete_records_do_not_hide_valid_evidence(
    tmp_path: Path, bad: bytes, category: str
) -> None:
    path = tmp_path / "events.ndjson"
    path.write_bytes(event_bytes() + bad + event_bytes())
    result = inspect(path)
    assert len(result.events) == 2
    assert not result.complete and not result.ordering_reliable
    assert [(item.category, item.line_number) for item in result.diagnostics] == [
        (category, 2)
    ]


def test_diagnostic_precedence_and_order_preserve_valid_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.ndjson"
    snapshot = (
        b"\n"
        + event_bytes(task_id="first")
        + b"\xff\n"
        + event_bytes(event_type="unknown", workflow_name="other", timestamp="invalid")
        + event_bytes(workflow_name="other", timestamp="invalid")
        + event_bytes(timestamp="invalid", log_file=42)
        + event_bytes(task_id="last")
        + b"\xff"
    )
    path.write_bytes(snapshot)

    result = inspect(path)

    assert [event.context.task_id for event in result.events] == ["first", "last"]
    assert result.diagnostics == (
        EventLogDiagnostic("encoding", "Invalid UTF-8 in saved event record.", 3),
        EventLogDiagnostic("malformed", "Malformed saved event record.", 4),
        EventLogDiagnostic(
            "identity", "Saved event workflow/run identity mismatch.", 5
        ),
        EventLogDiagnostic("invalid", "Invalid saved event timestamp.", 6),
        EventLogDiagnostic(
            "partial", "Unfinished trailing event append; retry inspection.", 8
        ),
    )
    assert result.partial and not result.complete and not result.ordering_reliable
    assert path.read_bytes() == snapshot


@pytest.mark.parametrize("operation", ["event_from_line", "inspection_event_problem"])
@pytest.mark.parametrize(
    "error_type", [ValueError, OverflowError, OSError, RecursionError, TypeError]
)
def test_record_exception_boundaries(
    tmp_path: Path, operation: str, error_type: type[Exception]
) -> None:
    path = tmp_path / "events.ndjson"
    path.write_bytes(event_bytes())

    with patch(
        f"crewplane.observability.events.reader.{operation}",
        side_effect=error_type("record failure"),
    ):
        if operation == "event_from_line" and error_type is not TypeError:
            result = inspect(path)
            assert result.events == ()
            assert result.diagnostics == (
                EventLogDiagnostic("malformed", "Malformed saved event record.", 1),
            )
        else:
            with pytest.raises(error_type, match="record failure"):
                inspect(path)


@pytest.mark.parametrize(
    "tail", [b'{"event_type":', b"\xe2\x82", event_bytes().rstrip(b"\n")]
)
def test_trailing_append_is_partial_not_trusted(tmp_path: Path, tail: bytes) -> None:
    path = tmp_path / "events.ndjson"
    path.write_bytes(event_bytes() + tail)
    result = inspect(path)
    assert len(result.events) == 1
    assert result.partial and not result.ordering_reliable
    assert result.diagnostics[0].line_number == 2


def test_missing_and_unsafe_logs_are_incomplete(tmp_path: Path) -> None:
    path = tmp_path / "missing.ndjson"
    assert inspect(path).diagnostics[0].category == "missing"
    path.mkdir()
    assert not inspect(path).complete
    source = tmp_path / "source"
    source.write_bytes(event_bytes())
    link = tmp_path / "link"
    symlink_or_skip(link, source)
    assert not inspect(link).complete
    hardlink = tmp_path / "hardlink"
    os.link(source, hardlink)
    assert not inspect(hardlink).complete
    assert not inspect(source).complete


def test_unreadable_log_reports_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(path: Path) -> bytes:
        raise PermissionError(path)

    monkeypatch.setattr(
        "crewplane.observability.events.reader.read_event_snapshot", denied
    )
    result = inspect(tmp_path / "events.ndjson")
    assert result.diagnostics[0].category == "unreadable"


@pytest.mark.race
def test_append_during_snapshot_is_bounded_and_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "events.ndjson"
    original = event_bytes()
    path.write_bytes(original)
    read = os.read
    appended = False

    def append_on_read(descriptor: int, size: int) -> bytes:
        nonlocal appended
        if not appended:
            appended = True
            with path.open("ab") as output:
                output.write(event_bytes(task_id="later"))
        return read(descriptor, size)

    monkeypatch.setattr(
        "crewplane.observability.events.inspection.os.read", append_on_read
    )
    result = inspect(path)
    assert result.complete
    assert len(result.events) == 1
    assert path.stat().st_size > len(original)


@pytest.mark.race
@pytest.mark.parametrize("change", ["truncate", "replace", "delete"])
def test_snapshot_mutation_is_not_clean_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    path = tmp_path / "events.ndjson"
    path.write_bytes(event_bytes())
    read = os.read
    changed = False

    def mutate_on_read(descriptor: int, size: int) -> bytes:
        nonlocal changed
        chunk = read(descriptor, size)
        if not changed:
            changed = True
            if change == "truncate":
                path.write_bytes(b"")
            elif change == "replace":
                replacement = tmp_path / "replacement"
                replacement.write_bytes(event_bytes())
                replacement.replace(path)
            else:
                path.unlink()
        return chunk

    monkeypatch.setattr(
        "crewplane.observability.events.inspection.os.read", mutate_on_read
    )
    result = inspect(path)
    assert not result.complete and not result.events


def test_tolerant_timestamp_behavior_is_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "events.ndjson"
    path.write_bytes(event_bytes(timestamp="invalid"))
    assert read_event_log(path)[0].timestamp == 0
    assert not inspect(path).complete
