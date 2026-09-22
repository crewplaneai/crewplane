from __future__ import annotations

import json
from pathlib import Path

import pytest

from crewplane.artifacts.run_history import RunHistoryRecord
from crewplane.cli.run import historical_summary
from crewplane.observability.events import RuntimeLogEventPayload, event_from_record
from crewplane.observability.events.types import EventType, LogLevel
from tests.helpers.resume import make_plan, make_run_manifest


@pytest.mark.parametrize("level", LogLevel)
def test_historical_log_reader_parses_each_supported_level(level: LogLevel) -> None:
    event = event_from_record(_runtime_log_record(level=level.value))

    assert event is not None
    assert isinstance(event.payload, RuntimeLogEventPayload)
    assert event.payload.level is level


def test_refresh_historical_summary_replays_valid_historical_events(
    tmp_path: Path,
) -> None:
    record = _history_record(tmp_path)
    event_log_path = record.run_dir / "logs" / "events.ndjson"
    event_log_path.parent.mkdir(parents=True, exist_ok=True)
    event_log_path.write_text(
        "\n".join(
            [
                json.dumps(_runtime_log_record(level="warning")),
                json.dumps(_base_record("unknown_event")),
                json.dumps(_runtime_log_record(level="verbose")),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary_path = historical_summary.refresh_historical_run_summary(
        make_plan(),
        record,
    )

    summary_text = summary_path.read_text(encoding="utf-8")
    assert summary_path == record.run_dir / "logs" / "summary.md"
    assert "valid historical warning" in summary_text
    assert "ignored unknown event" not in summary_text
    assert "ignored invalid log level" not in summary_text


def test_historical_log_lookup_does_not_create_missing_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _history_record(tmp_path)
    read_event_log = historical_summary.read_event_log

    def read_missing_log(path: Path):
        assert path == record.run_dir / "logs" / "events.ndjson"
        assert not path.parent.exists()
        return read_event_log(path)

    monkeypatch.setattr(historical_summary, "read_event_log", read_missing_log)
    summary_path = historical_summary.refresh_historical_run_summary(
        make_plan(), record
    )
    assert summary_path == record.run_dir / "logs" / "summary.md"
    assert summary_path.is_file()
    assert not (record.run_dir / "logs" / "events.ndjson").exists()


def test_refresh_historical_summary_handles_each_declared_event_type(
    tmp_path: Path,
) -> None:
    record = _history_record(tmp_path)
    event_log_path = record.run_dir / "logs" / "events.ndjson"
    event_log_path.parent.mkdir(parents=True, exist_ok=True)
    event_log_path.write_text(
        "\n".join(
            json.dumps(_record_for_event(event_type)) for event_type in tuple(EventType)
        )
        + "\n",
        encoding="utf-8",
    )

    summary_path = historical_summary.refresh_historical_run_summary(
        make_plan(),
        record,
    )

    assert "Run Summary" in summary_path.read_text(encoding="utf-8")


def _record_for_event(event_type: EventType) -> dict[str, object]:
    if event_type == EventType.RUNTIME_LOG:
        return _runtime_log_record(level="info")
    return _base_record(event_type.value)


def _runtime_log_record(level: str) -> dict[str, object]:
    record = _base_record("runtime_log")
    record.update(
        {
            "level": level,
            "message": (
                "ignored invalid log level"
                if level == "verbose"
                else "valid historical warning"
            ),
            "operation": "summary_replay",
        }
    )
    return record


def _base_record(event_type: str) -> dict[str, object]:
    return {
        "event_type": event_type,
        "workflow_name": "workflow",
        "run_id": "run-1",
        "timestamp": "2026-06-22T00:00:00+00:00",
        "error": "ignored unknown event" if event_type == "unknown_event" else None,
    }


def _history_record(tmp_path: Path) -> RunHistoryRecord:
    manifest = make_run_manifest(
        run_id="run-1",
        run_key_name="workflow--run-1",
        status="succeeded",
    )
    run_dir = tmp_path / "execution-stages" / manifest.run_key_name
    results_dir = tmp_path / "execution-results" / manifest.run_key_name
    manifest_path = run_dir / "manifests" / "run.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        manifest.model_dump_json(exclude_none=True),
        encoding="utf-8",
    )
    results_dir.mkdir(parents=True, exist_ok=True)
    return RunHistoryRecord(
        manifest=manifest,
        manifest_path=manifest_path,
        run_dir=run_dir,
        results_dir=results_dir,
    )
