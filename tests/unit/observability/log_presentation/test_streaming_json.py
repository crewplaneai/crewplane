from pathlib import Path

import pytest

from crewplane.architecture.contracts import LogPresentationDescriptor
from crewplane.observability.log_presentation import (
    format_log_file,
    formatters,
    throttle,
)
from crewplane.observability.log_presentation.throttle import (
    IncompleteJsonObjectThrottle,
)


@pytest.fixture
def parse_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    clock = [10.0]

    def current_time() -> float:
        return clock[0]

    monkeypatch.setattr(throttle, "monotonic", current_time)
    monkeypatch.setattr(
        formatters, "JSON_OBJECT_THROTTLE", IncompleteJsonObjectThrottle()
    )
    return clock


@pytest.mark.parametrize("profile", ["generic", "claude"])
def test_streaming_object_waits_for_parse_interval_then_displays_completed_output(
    tmp_path: Path,
    parse_clock: list[float],
    profile: str,
) -> None:
    log = tmp_path / "provider.log"
    descriptor = LogPresentationDescriptor(format="json_object", profile=profile)
    log.write_text('{"result":', encoding="utf-8")

    partial = format_log_file(log, descriptor, line_budget=5)
    assert partial.notices[0].message == "Could not parse structured provider log."

    log.write_text('{"result":"finished"}', encoding="utf-8")
    parse_clock[0] += 0.5
    waiting = format_log_file(log, descriptor, line_budget=5)
    assert waiting.notices[0].message == "Waiting for complete JSON object..."

    parse_clock[0] += 0.5
    completed = format_log_file(log, descriptor, line_budget=5)
    assert completed.lines == ("result: finished",)
    assert completed.notices == ()

    log.write_text('{"result":"next output"}', encoding="utf-8")
    refreshed = format_log_file(log, descriptor, line_budget=5)
    assert refreshed.lines == ("result: next output",)
    assert refreshed.notices == ()


def test_successful_invocation_displays_final_object_without_waiting(
    tmp_path: Path,
    parse_clock: list[float],
) -> None:
    log = tmp_path / "provider.log"
    descriptor = LogPresentationDescriptor(format="json_object", profile="claude")
    log.write_text('{"result":', encoding="utf-8")
    format_log_file(log, descriptor, line_budget=5)
    log.write_text('{"result":"done"}', encoding="utf-8")

    final = format_log_file(
        log, descriptor, line_budget=5, invocation_status="succeeded"
    )

    assert parse_clock == [10.0]
    assert final.lines == ("result: done",)
    assert final.notices == ()


def test_truncated_log_starts_a_new_parse_without_waiting(
    tmp_path: Path,
    parse_clock: list[float],
) -> None:
    log = tmp_path / "provider.log"
    descriptor = LogPresentationDescriptor(format="json_object", profile="claude")
    log.write_text('{"result":"previous attempt", "usage":', encoding="utf-8")
    partial = format_log_file(log, descriptor, line_budget=5)
    assert partial.notices[0].message == "Could not parse structured provider log."
    log.write_text('{"result":"new"}', encoding="utf-8")

    restarted = format_log_file(log, descriptor, line_budget=5)

    assert parse_clock == [10.0]
    assert restarted.lines == ("result: new",)
    assert restarted.notices == ()


def test_incomplete_log_does_not_throttle_another_invocation(
    tmp_path: Path,
    parse_clock: list[float],
) -> None:
    descriptor = LogPresentationDescriptor(format="json_object", profile="claude")
    first = tmp_path / "first.log"
    second = tmp_path / "second.log"
    first.write_text('{"result":', encoding="utf-8")
    format_log_file(first, descriptor, line_budget=5)
    second.write_text('{"result":"independent"}', encoding="utf-8")

    snapshot = format_log_file(second, descriptor, line_budget=5)

    assert parse_clock == [10.0]
    assert snapshot.lines == ("result: independent",)
    assert snapshot.notices == ()
