from __future__ import annotations

import json
from pathlib import Path

import pytest

from crewplane.architecture.contracts import LogPresentationDescriptor
from crewplane.observability.log_presentation import (
    JSON_OBJECT_THROTTLE,
    LogPresentationLimits,
    format_log_file,
    formatters,
)
from crewplane.observability.log_presentation.json_extract import (
    exceeds_json_depth,
)


def test_json_lines_parses_final_record_without_trailing_newline(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "codex.log"
    log_path.write_text('{"message":"done"}', encoding="utf-8")

    snapshot = format_log_file(
        log_path,
        LogPresentationDescriptor(format="json_lines", profile="codex"),
        line_budget=5,
        wall_time_now=0.0,
    )

    assert snapshot.lines == ("done",)


def test_json_lines_drops_truncated_first_record(tmp_path: Path) -> None:
    log_path = tmp_path / "codex.log"
    log_path.write_text(
        '{"message":"this record starts before the bounded tail"}\n'
        '{"message":"keep"}\n',
        encoding="utf-8",
    )

    snapshot = format_log_file(
        log_path,
        LogPresentationDescriptor(format="json_lines", profile="codex"),
        line_budget=5,
        wall_time_now=0.0,
        limits=LogPresentationLimits(max_tail_bytes=24),
    )

    assert snapshot.lines == ("keep",)


def test_json_lines_keeps_first_record_when_tail_starts_on_line_boundary(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "codex.log"
    keep_record = '{"message":"keep"}\n'
    log_path.write_text(
        '{"message":"previous"}\n' + keep_record,
        encoding="utf-8",
    )

    snapshot = format_log_file(
        log_path,
        LogPresentationDescriptor(format="json_lines", profile="codex"),
        line_budget=5,
        wall_time_now=0.0,
        limits=LogPresentationLimits(max_tail_bytes=len(keep_record.encode("utf-8"))),
    )

    assert snapshot.lines == ("keep",)


def test_json_lines_renders_stderr_non_json_as_stderr(tmp_path: Path) -> None:
    log_path = tmp_path / "codex.log"
    log_path.write_text("[stderr] provider warning\n", encoding="utf-8")

    snapshot = format_log_file(
        log_path,
        LogPresentationDescriptor(format="json_lines", profile="codex"),
        line_budget=5,
        wall_time_now=0.0,
    )

    assert snapshot.lines == ("stderr: provider warning",)


def test_json_lines_expands_decoded_record_newlines(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "codex.log"
    log_path.write_text(
        '{"message":"first\\nsecond\\rthird"}\n',
        encoding="utf-8",
    )

    snapshot = format_log_file(
        log_path,
        LogPresentationDescriptor(format="json_lines", profile="codex"),
        line_budget=5,
        wall_time_now=0.0,
    )

    assert snapshot.lines == ("first", "  second third")


def test_json_lines_keeps_literal_backslash_n_on_one_display_line(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "codex.log"
    log_path.write_text(
        '{"message":"first\\\\nsecond"}\n',
        encoding="utf-8",
    )

    snapshot = format_log_file(
        log_path,
        LogPresentationDescriptor(format="json_lines", profile="codex"),
        line_budget=5,
        wall_time_now=0.0,
    )

    assert snapshot.lines == ("first\\nsecond",)


def test_json_lines_counts_expanded_physical_lines_against_budget(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "codex.log"
    log_path.write_text(
        '{"message":"first\\nsecond\\nthird"}\n',
        encoding="utf-8",
    )

    snapshot = format_log_file(
        log_path,
        LogPresentationDescriptor(format="json_lines", profile="codex"),
        line_budget=2,
        wall_time_now=0.0,
    )

    assert snapshot.lines == ("  second", "  third")


def test_json_lines_skips_oversized_records(tmp_path: Path) -> None:
    log_path = tmp_path / "codex.log"
    log_path.write_text('{"message":"too large"}\n', encoding="utf-8")

    snapshot = format_log_file(
        log_path,
        LogPresentationDescriptor(format="json_lines", profile="codex"),
        line_budget=5,
        wall_time_now=0.0,
        limits=LogPresentationLimits(max_jsonl_record_bytes=8),
    )

    assert snapshot.lines == ()
    assert snapshot.notices[0].message == "Skipped oversized JSONL record."


def test_json_lines_skips_deeply_nested_record(tmp_path: Path) -> None:
    log_path = tmp_path / "codex.log"
    log_path.write_text(_nested_json_object(depth=1_200), encoding="utf-8")

    snapshot = format_log_file(
        log_path,
        LogPresentationDescriptor(format="json_lines", profile="codex"),
        line_budget=5,
        wall_time_now=0.0,
    )

    assert snapshot.lines == ()
    assert snapshot.notices[0].message == "Skipped deeply nested JSONL record."


def test_json_lines_parse_recursion_renders_deep_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_path = tmp_path / "codex.log"
    log_path.write_text('{"message":"ok"}\n', encoding="utf-8")
    monkeypatch.setattr(formatters.json, "loads", _raise_recursion_error)

    snapshot = format_log_file(
        log_path,
        LogPresentationDescriptor(format="json_lines", profile="codex"),
        line_budget=5,
        wall_time_now=0.0,
    )

    assert snapshot.lines == ()
    assert snapshot.notices[0].message == "Skipped deeply nested JSONL record."


def test_codex_json_lines_render_item_events_without_raw_payload(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "codex.log"
    records = [
        {
            "type": "item.started",
            "item": {
                "id": "ws_123",
                "type": "web_search",
                "query": "",
                "action": {"type": "other"},
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "ws_123",
                "type": "web_search",
                "query": "original prompt fragment",
                "action": {
                    "type": "search",
                    "queries": [
                        ("site:developers.openai.com/codex/cli/reference codex exec")
                    ],
                },
            },
        },
    ]
    log_path.write_text(
        "\n".join(json.dumps(record) for record in records),
        encoding="utf-8",
    )

    snapshot = format_log_file(
        log_path,
        LogPresentationDescriptor(format="json_lines", profile="codex"),
        line_budget=5,
        wall_time_now=0.0,
    )

    assert snapshot.lines == (
        "web_search started",
        "web_search completed: site:developers.openai.com/codex/cli/reference codex exec",
    )
    assert not any("ws_123" in line for line in snapshot.lines)
    assert not any('"item"' in line for line in snapshot.lines)


def test_codex_json_lines_renders_nested_item_text(tmp_path: Path) -> None:
    log_path = tmp_path / "codex.log"
    log_path.write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "msg_123",
                    "type": "agent_message",
                    "text": "Nested answer text",
                },
            }
        ),
        encoding="utf-8",
    )

    snapshot = format_log_file(
        log_path,
        LogPresentationDescriptor(format="json_lines", profile="codex"),
        line_budget=5,
        wall_time_now=0.0,
    )

    assert snapshot.lines == ("agent_message completed: Nested answer text",)


def test_codex_json_lines_renders_nested_tool_details(tmp_path: Path) -> None:
    log_path = tmp_path / "codex.log"
    log_path.write_text(
        json.dumps(
            {
                "type": "item.completed",
                "status": "completed",
                "exit_code": 0,
                "item": {
                    "type": "local_shell",
                    "command": "uv run python -m pytest -q tests/unit",
                    "stdout": "2 passed",
                },
            }
        ),
        encoding="utf-8",
    )

    snapshot = format_log_file(
        log_path,
        LogPresentationDescriptor(format="json_lines", profile="codex"),
        line_budget=5,
        wall_time_now=0.0,
    )

    assert snapshot.lines == (
        "local_shell completed: "
        "command: uv run python -m pytest -q tests/unit | "
        "status: completed | exit_code: 0 | stdout: 2 passed",
    )


def test_codex_json_lines_preserves_command_execution_output_lines(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "codex.log"
    log_path.write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "nl -ba scripts/release/state.py | sed -n '940,942p'",
                    "status": "completed",
                    "exit_code": 0,
                    "aggregated_output": (
                        "940     expected = {\n"
                        '941         "package_name": context.package_name,\n'
                        "942     }"
                    ),
                },
            }
        ),
        encoding="utf-8",
    )

    snapshot = format_log_file(
        log_path,
        LogPresentationDescriptor(format="json_lines", profile="codex"),
        line_budget=5,
        wall_time_now=0.0,
    )

    assert snapshot.lines == (
        "command_execution completed: "
        "command: nl -ba scripts/release/state.py | sed -n '940,942p' | "
        "status: completed | exit_code: 0",
        "aggregated_output: 940     expected = {",
        '  941         "package_name": context.package_name,',
        "  942     }",
    )


def test_codex_json_lines_renders_observed_aggregated_output_snippet(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "codex.log"
    log_path.write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "aggregated_output": "Observed transcript output",
                },
            }
        ),
        encoding="utf-8",
    )

    snapshot = format_log_file(
        log_path,
        LogPresentationDescriptor(format="json_lines", profile="codex"),
        line_budget=5,
        wall_time_now=0.0,
    )

    assert snapshot.lines == (
        "agent_message completed: aggregated_output: Observed transcript output",
    )


def test_codex_json_lines_unknown_item_shape_falls_back_safely(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "codex.log"
    log_path.write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "opaque_123",
                    "type": "unknown_shape",
                    "action": {"type": "opaque"},
                    "metadata": {"opaque": True},
                },
            }
        ),
        encoding="utf-8",
    )

    snapshot = format_log_file(
        log_path,
        LogPresentationDescriptor(format="json_lines", profile="codex"),
        line_budget=5,
        wall_time_now=0.0,
    )

    assert len(snapshot.lines) == 1
    assert snapshot.lines[0].startswith("item.completed: {")
    assert "unknown_shape" in snapshot.lines[0]
    assert snapshot.notices == ()


def test_codex_json_lines_sanitizes_and_clips_nested_item_content(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "codex.log"
    log_path.write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "first\nsecond\rthird " + ("x" * 80),
                },
            }
        ),
        encoding="utf-8",
    )

    snapshot = format_log_file(
        log_path,
        LogPresentationDescriptor(format="json_lines", profile="codex"),
        line_budget=5,
        wall_time_now=0.0,
        limits=LogPresentationLimits(max_display_chars_per_record=56),
    )

    assert len(snapshot.lines) == 1
    assert len(snapshot.lines[0]) == 56
    assert "\n" not in snapshot.lines[0]
    assert "\r" not in snapshot.lines[0]
    assert snapshot.lines[0].startswith("agent_message completed: first second")
    assert snapshot.lines[0].endswith("...")


def test_claude_json_object_parses_stderr_lines_and_redacts(tmp_path: Path) -> None:
    log_path = tmp_path / "claude.log"
    log_path.write_text(
        '[stderr] {"result":"ok","usage":{"input":1,"api_key":"secret"}}\n',
        encoding="utf-8",
    )

    snapshot = format_log_file(
        log_path,
        LogPresentationDescriptor(format="json_object", profile="claude"),
        line_budget=5,
        wall_time_now=0.0,
        invocation_status="succeeded",
    )

    assert snapshot.lines[0] == "result: ok"
    assert '"api_key": "[redacted]"' in snapshot.lines[1]


def test_json_object_expands_decoded_labeled_field_newlines(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "claude.log"
    log_path.write_text(
        '{"result":"first\\nsecond\\rthird"}\n',
        encoding="utf-8",
    )

    snapshot = format_log_file(
        log_path,
        LogPresentationDescriptor(format="json_object", profile="claude"),
        line_budget=5,
        wall_time_now=0.0,
        invocation_status="succeeded",
    )

    assert snapshot.lines == ("result: first", "  second third")


def test_json_object_falls_back_for_deeply_nested_input(tmp_path: Path) -> None:
    log_path = tmp_path / "claude.log"
    log_path.write_text(_nested_json_object(depth=1_200), encoding="utf-8")

    snapshot = format_log_file(
        log_path,
        LogPresentationDescriptor(format="json_object", profile="claude"),
        line_budget=5,
        wall_time_now=0.0,
        invocation_status="succeeded",
    )

    assert snapshot.notices[0].message == (
        "Structured provider log is too deeply nested."
    )


def test_json_object_parse_recursion_renders_deep_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_path = tmp_path / "claude.log"
    log_path.write_text('{"result":"ok"}\n', encoding="utf-8")
    monkeypatch.setattr(formatters.json, "loads", _raise_recursion_error)

    snapshot = format_log_file(
        log_path,
        LogPresentationDescriptor(format="json_object", profile="claude"),
        line_budget=5,
        wall_time_now=0.0,
        invocation_status="succeeded",
    )

    assert snapshot.notices[0].message == (
        "Structured provider log is too deeply nested."
    )


def test_depth_check_is_iterative_for_extreme_provider_json() -> None:
    current: object = "leaf"
    remaining_depth = 2_000
    while remaining_depth:
        current = [current]
        remaining_depth -= 1

    assert exceeds_json_depth(current, max_depth=12)


def test_claude_json_object_recovers_diagnostics_around_object(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "claude.log"
    log_path.write_text(
        "\n".join(
            [
                "[stderr] warming up",
                '[stderr] {"result":"ok","usage":{"input":1}}',
                "[stderr] cleanup warning",
            ]
        ),
        encoding="utf-8",
    )

    snapshot = format_log_file(
        log_path,
        LogPresentationDescriptor(format="json_object", profile="claude"),
        line_budget=5,
        wall_time_now=0.0,
        invocation_status="succeeded",
    )

    assert snapshot.lines[0] == "stderr: warming up"
    assert snapshot.lines[1] == "stderr: cleanup warning"
    assert "result: ok" in snapshot.lines


def test_claude_json_object_recovers_inner_stderr_diagnostic_line(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "claude.log"
    log_path.write_text(
        "\n".join(
            [
                "[stderr] {",
                '[stderr] "result": "ok",',
                "[stderr] transient provider diagnostic",
                '[stderr] "usage": {"input": 1}',
                "[stderr] }",
            ]
        ),
        encoding="utf-8",
    )

    snapshot = format_log_file(
        log_path,
        LogPresentationDescriptor(format="json_object", profile="claude"),
        line_budget=5,
        wall_time_now=0.0,
        invocation_status="succeeded",
    )

    assert snapshot.lines[0] == "stderr: transient provider diagnostic"
    assert "result: ok" in snapshot.lines


def test_json_object_oversize_clears_incomplete_parse_throttle(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "claude.log"
    log_path.write_text('{"result":"partial"', encoding="utf-8")
    JSON_OBJECT_THROTTLE.mark_incomplete(log_path, size_bytes=10, now=0.0)
    log_path.write_text("x" * 32, encoding="utf-8")

    snapshot = format_log_file(
        log_path,
        LogPresentationDescriptor(format="json_object", profile="claude"),
        line_budget=5,
        wall_time_now=0.0,
        invocation_status="running",
        limits=LogPresentationLimits(max_json_object_parse_bytes=8),
    )

    assert snapshot.notices[0].message == (
        "Structured provider log exceeds display parse limit."
    )
    assert JSON_OBJECT_THROTTLE.should_parse(
        log_path,
        status="running",
        size_bytes=32,
        now=0.0,
    )


def test_plain_formatter_strips_recognized_header(tmp_path: Path) -> None:
    log_path = tmp_path / "plain.log"
    log_path.write_text(
        "\n".join(
            [
                "started_at: 2026-06-10T00:00:00+00:00",
                "cli_executable: provider",
                "model: test",
                f"output_file: {tmp_path / 'out.md'}",
                "---",
                "visible",
            ]
        ),
        encoding="utf-8",
    )

    snapshot = format_log_file(
        log_path,
        LogPresentationDescriptor(format="plain", profile="generic"),
        line_budget=5,
        wall_time_now=0.0,
    )

    assert snapshot.lines == ("visible",)


def _nested_json_object(depth: int) -> str:
    return '{"message":' * depth + '"leaf"' + "}" * depth + "\n"


def _raise_recursion_error(value: str) -> object:
    raise RecursionError(f"recursive parse for {len(value)} bytes")
