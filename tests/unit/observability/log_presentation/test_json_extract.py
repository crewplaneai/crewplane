import pytest

from crewplane.observability.log_presentation.json_extract import (
    compact_json_line,
    display_string_lines,
    exceeds_json_depth,
    render_json_object,
    render_json_record,
)
from crewplane.observability.log_presentation.limits import LogPresentationLimits

LIMITS = LogPresentationLimits(max_display_chars_per_record=200)


def test_depth_check_walks_mappings_and_lists() -> None:
    assert not exceeds_json_depth({"items": [1, {"value": 2}]}, max_depth=4)
    assert exceeds_json_depth({"items": [1, {"value": 2}]}, max_depth=3)


def test_compact_json_line_redacts_and_falls_back_for_non_json_values() -> None:
    assert compact_json_line({"token": "secret", "value": 1}, LIMITS) == (
        '{"token": "[redacted]", "value": 1}'
    )
    assert compact_json_line({1, 2}, LIMITS) in {"{1, 2}", "{2, 1}"}


def test_non_mapping_records_render_compactly() -> None:
    assert render_json_record([1, 2], "generic", LIMITS) == ["[1, 2]"]
    assert render_json_object("value", "claude", LIMITS) == ['"value"']


def test_mock_record_renders_known_fields_or_compact_fallback() -> None:
    assert render_json_record(
        {"source": "fixture", "node_id": "build", "secret": "hidden"},
        "mock",
        LIMITS,
    ) == ["mock: source=fixture node_id=build"]
    assert render_json_record({"unknown": 1}, "mock", LIMITS) == [
        'mock: {"unknown": 1}'
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("message", "direct", id="message"),
        pytest.param("content", "direct", id="content"),
        pytest.param("text", "direct", id="text"),
        pytest.param("delta", "direct", id="delta"),
    ],
)
def test_codex_direct_content_fields_render_as_text(field: str, value: str) -> None:
    assert render_json_record({field: value}, "codex", LIMITS) == [value]


def test_codex_command_event_expands_multiline_output_with_metadata() -> None:
    record = {
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "command": ["pytest", "-q"],
            "aggregated_output": "first\nsecond",
        },
        "status": "failed",
        "exit_code": 1,
    }

    assert render_json_record(record, "codex", LIMITS) == [
        'command_execution completed: command: ["pytest", "-q"] | '
        "status: failed | exit_code: 1",
        "aggregated_output: first",
        "  second",
    ]


def test_codex_item_event_combines_search_text_status_and_outputs() -> None:
    record = {
        "event": "item.completed",
        "item": {
            "type": "web_search",
            "action": {"queries": ["", "crewplane"]},
            "content": "found",
            "status": "done",
            "stdout": {"matches": 2},
        },
    }

    assert render_json_record(record, "codex", LIMITS) == [
        'web_search completed: crewplane | found | status: done | stdout: {"matches": 2}'
    ]


def test_codex_empty_web_search_and_unknown_event_have_stable_labels() -> None:
    assert render_json_record(
        {"type": "item.started", "item": {"type": "web_search"}},
        "codex",
        LIMITS,
    ) == ["web_search started"]
    assert render_json_record(
        {"type": "turn.completed", "status": 0},
        "codex",
        LIMITS,
    ) == ["turn.completed: status: 0"]
    assert render_json_record(
        {"type": "item.completed", "item": "invalid"},
        "codex",
        LIMITS,
    )[0].startswith("item.completed: {")


def test_codex_bare_item_uses_item_detail_or_generic_fallback() -> None:
    assert render_json_record(
        {"item": {"query": "docs", "result": True}},
        "codex",
        LIMITS,
    ) == ["item: docs | result: True"]
    assert render_json_record({"item": {}}, "codex", LIMITS) == ['{"item": {}}']


def test_claude_object_renders_results_metadata_and_non_string_errors() -> None:
    record = {
        "result": "first\nsecond",
        "error": {"code": 3},
        "usage": {"input": 2},
        "total_cost_usd": 0.5,
        "duration_ms": 10,
        "num_turns": 2,
    }

    assert render_json_object(record, "claude", LIMITS) == [
        "result: first",
        "  second",
        "error: {'code': 3}",
        'usage: {"input": 2}',
        "total_cost_usd: 0.5",
        "duration_ms: 10",
        "num_turns: 2",
    ]


def test_claude_and_generic_objects_fall_back_when_content_is_empty() -> None:
    assert render_json_object({}, "claude", LIMITS) == ["{}"]
    assert render_json_object({"message": "visible"}, "generic", LIMITS) == [
        "message: visible"
    ]
    assert render_json_record({"message": "  "}, "generic", LIMITS) == [
        '{"message": "  "}'
    ]


def test_display_string_lines_normalizes_newlines_labels_and_blank_values() -> None:
    assert display_string_lines(" \n ", LIMITS, label="result") == []
    assert display_string_lines("first\r\nsecond\rthird", LIMITS, label="result") == [
        "result: first",
        "  second third",
    ]
