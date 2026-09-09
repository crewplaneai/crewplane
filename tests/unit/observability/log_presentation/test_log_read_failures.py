from __future__ import annotations

import sys
from pathlib import Path

import pytest

from crewplane.architecture.contracts import (
    LogPresentationDescriptor,
    LogPresentationFormat,
)
from crewplane.observability.log_presentation import (
    LogPresentationLimits,
    format_log_file,
)
from crewplane.observability.log_presentation.tail import latest_attempt_bytes


@pytest.mark.parametrize("presentation", ["plain", "json_lines", "json_object"])
@pytest.mark.parametrize("path_kind", ["missing", "directory"])
def test_unreadable_log_returns_notice(
    tmp_path: Path, presentation: LogPresentationFormat, path_kind: str
) -> None:
    log = tmp_path / "provider.log"
    if path_kind == "directory":
        log.mkdir()

    snapshot = format_log_file(log, LogPresentationDescriptor(presentation), 10)

    assert snapshot.lines == ()
    assert snapshot.size_bytes == 0
    assert snapshot.updated_age_seconds is None
    assert [notice.message for notice in snapshot.notices] == ["Log file unavailable."]


def test_plain_tail_discards_partial_first_line(tmp_path: Path) -> None:
    log = tmp_path / "provider.log"
    log.write_text("previous line is truncated\nkeep\n", encoding="utf-8")

    snapshot = format_log_file(
        log,
        LogPresentationDescriptor("plain"),
        10,
        limits=LogPresentationLimits(max_tail_bytes=12),
    )

    assert snapshot.lines == ("keep",)
    assert snapshot.truncated


def test_json_lines_preserves_plain_diagnostics_and_ignores_blank_lines(
    tmp_path: Path,
) -> None:
    log = tmp_path / "provider.log"
    log.write_text('\n  \nprovider starting\n{"message":"done"}\n', encoding="utf-8")

    snapshot = format_log_file(log, LogPresentationDescriptor("json_lines"), 10)

    assert snapshot.lines == ("provider starting", "message: done")
    assert snapshot.notices == ()


@pytest.mark.parametrize(
    "body",
    [
        "",
        "diagnostic only",
        '{"result":invalid}',
        '{\n"result": invalid\n}',
        '{\n[stderr] diagnostic\n"result": invalid\n}',
        'diagnostic\n{"unknown":"value"}',
    ],
    ids=["empty", "plain", "invalid-value", "multiline", "inner-warning", "unknown"],
)
def test_unrecoverable_claude_output_keeps_raw_text_and_warns(
    tmp_path: Path, body: str
) -> None:
    log = tmp_path / "provider.log"
    log.write_text(body, encoding="utf-8")

    snapshot = format_log_file(
        log,
        LogPresentationDescriptor("json_object", "claude"),
        10,
        invocation_status="failed",
    )

    assert snapshot.lines == tuple(body.splitlines())
    assert [notice.message for notice in snapshot.notices] == [
        "Could not parse structured provider log."
    ]


def test_json_integer_limit_falls_back_without_crashing(tmp_path: Path) -> None:
    log = tmp_path / "provider.log"
    log.write_text('{"result":' + "9" * 4301 + "}", encoding="utf-8")
    previous_limit = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(4300)
        snapshot = format_log_file(
            log,
            LogPresentationDescriptor("json_object"),
            10,
            invocation_status="failed",
        )
    finally:
        sys.set_int_max_str_digits(previous_limit)

    assert snapshot.lines[0].startswith('{"result":999')
    assert snapshot.lines[0].endswith("...")
    assert snapshot.notices[0].message == "Could not parse structured provider log."


def test_claude_inner_diagnostic_recovery_preserves_blank_json_lines(
    tmp_path: Path,
) -> None:
    log = tmp_path / "provider.log"
    log.write_text(
        '[stderr] before\n{\n[stderr] \n[stderr] warning\n"result":"ok"\n}\nafter',
        encoding="utf-8",
    )

    snapshot = format_log_file(
        log,
        LogPresentationDescriptor("json_object", "claude"),
        10,
        invocation_status="succeeded",
    )

    assert snapshot.lines == (
        "stderr: before",
        "stderr: after",
        "stderr: warning",
        "result: ok",
    )
    assert snapshot.notices == ()


@pytest.mark.parametrize("truncated", [False, True])
def test_partial_retry_header_preserves_truncation(truncated: bool) -> None:
    result = latest_attempt_bytes(b"previous\n---\nretry_attempt: 2\n", truncated)

    assert result.body == b"\n---\nretry_attempt: 2\n"
    assert result.truncated is truncated
