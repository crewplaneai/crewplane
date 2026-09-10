from __future__ import annotations

import pytest

from crewplane.observability.log_presentation.json_extract import (
    render_json_object,
    render_json_record,
)


@pytest.mark.parametrize(
    ("profile", "record", "expected"),
    [
        ("generic", [1, "done"], ['[1, "done"]']),
        ("codex", {"item": {"text": "answer"}}, ["item: answer"]),
        ("codex", {"item": {}}, ['{"item": {}}']),
        ("codex", {"type": "item.started"}, ['item.started: {"type": "item.started"}']),
        ("codex", {"type": "command", "exit_code": 0}, ["command: exit_code: 0"]),
        (
            "kilo",
            {"type": "text", "part": "broken"},
            ['{"part": "broken", "type": "text"}'],
        ),
        (
            "kilo",
            {"type": "text", "part": {"text": ""}},
            ['{"part": {"text": ""}, "type": "text"}'],
        ),
        ("codex", {"item": {"output": {"ok": True}}}, ['item: output: {"ok": true}']),
        (
            "codex",
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "aggregated_output": "one\ntwo"},
            },
            ["command_execution completed", "aggregated_output: one", "  two"],
        ),
    ],
)
def test_provider_record_variants_have_readable_fallbacks(
    profile: str, record: object, expected: list[str]
) -> None:
    assert render_json_record(record, profile) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ([1], ["[1]"]),
        ({"error": 429}, ["error: 429"]),
        ({"other": True}, ['{"other": true}']),
    ],
)
def test_claude_nontext_values_remain_visible(
    value: object, expected: list[str]
) -> None:
    assert render_json_object(value, "claude") == expected
