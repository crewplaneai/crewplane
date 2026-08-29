from __future__ import annotations

from pathlib import Path

import pytest

from crewplane.adapters.invokers.cli_invoker import (
    claude_json,
    claude_json_parser,
)
from crewplane.adapters.invokers.cli_invoker.claude_json_parser import (
    parse_claude_result,
)
from crewplane.architecture.contracts import CommandResult


@pytest.mark.parametrize(
    ("encoded_result", "expected_result", "expected_count"),
    [
        ("\\uD83D\\uDE00", "😀", 1),
        ("\\uD800", "\ufffd", 1),
        ("\\uDC00", "\ufffd", 1),
        ("\\uD800\\u0041", "\ufffdA", 2),
    ],
)
def test_result_parser_decodes_surrogate_escapes(
    tmp_path: Path,
    encoded_result: str,
    expected_result: str,
    expected_count: int,
) -> None:
    output_path = tmp_path / "result.txt"

    char_count = parse_claude_result(
        (f'{{"result":"{encoded_result}"}}',),
        output_path,
    )

    assert output_path.read_text(encoding="utf-8") == expected_result
    assert char_count == expected_count


def test_result_parser_accepts_empty_intermediate_chunks(tmp_path: Path) -> None:
    output_path = tmp_path / "result.txt"

    char_count = parse_claude_result(
        ("", '{"res', "", 'ult":"o', "", 'k"}', ""),
        output_path,
    )

    assert char_count == 2
    assert output_path.read_text(encoding="utf-8") == "ok"


def test_usage_parser_normalizes_integer_conversion_limits() -> None:
    oversized_integer = "1" * 5_000

    usage, error = claude_json.read_claude_model_usage(
        CommandResult(0, f'{{"modelUsage":{oversized_integer}}}', ""),
        10_000,
    )

    assert usage is None
    assert error == "Malformed Claude JSON output."


@pytest.mark.parametrize(
    ("model_usage", "byte_limit", "expected_usage"),
    [
        ('"é"', 4, "é"),
        ('"é"', 3, None),
        ('"\\u00e9"', 8, "é"),
        ('"\\u00e9"', 7, None),
    ],
)
def test_usage_parser_counts_raw_utf8_capture_bytes(
    model_usage: str,
    byte_limit: int,
    expected_usage: object | None,
) -> None:
    usage, error = claude_json.read_claude_model_usage(
        CommandResult(0, f'{{"modelUsage":{model_usage}}}', ""),
        byte_limit,
    )

    assert usage == expected_usage
    assert (error is None) is (expected_usage is not None)


def test_usage_parser_applies_capture_limit_across_duplicate_fields() -> None:
    result = CommandResult(0, '{"modelUsage":1,"modelUsage":2}', "")

    within_limit = claude_json.read_claude_model_usage(result, 2)
    over_limit = claude_json.read_claude_model_usage(result, 1)

    assert within_limit == (2, None)
    assert over_limit == (None, "Malformed Claude JSON output.")


def test_usage_parser_normalizes_decoder_recursion_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_recursion_error(payload: str) -> object:
        assert payload == "{}"
        raise RecursionError

    monkeypatch.setattr(claude_json_parser.json, "loads", raise_recursion_error)

    usage, error = claude_json.read_claude_model_usage(
        CommandResult(0, '{"modelUsage":{}}', ""),
        10,
    )

    assert usage is None
    assert error == "Malformed Claude JSON output."
