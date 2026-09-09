from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path

import pytest

from crewplane.adapters.invokers.cli_invoker import (
    claude_json,
    machine_json,
)
from crewplane.adapters.invokers.cli_invoker.machine_json import (
    extract_claude_output,
    extract_codex_output,
    extract_gemini_output,
    extract_kilo_output,
)
from crewplane.architecture.contracts import CommandResult

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "provider_usage"


def test_codex_output_extractor_uses_last_message_file(tmp_path: Path) -> None:
    output_path = tmp_path / "last-message.txt"
    output_path.write_text("Codex response", encoding="utf-8")

    extracted = extract_codex_output(
        CommandResult(0, "ignored", ""),
        output_path,
    )

    assert extracted.output_extraction_status == "success"
    assert extracted.output_path == output_path
    assert extracted.output_char_count == len("Codex response")


@pytest.mark.parametrize("content", ["", "   \n\t"])
def test_codex_output_extractor_reports_missing_file_content(
    tmp_path: Path,
    content: str,
) -> None:
    output_path = tmp_path / "last-message.txt"
    output_path.write_text(content, encoding="utf-8")

    extracted = extract_codex_output(CommandResult(0, "ignored", ""), output_path)

    assert extracted.output_extraction_status == "missing"


def test_claude_output_extractor_streams_result_without_loading_whole_document() -> (
    None
):
    extracted = extract_claude_output(
        CommandResult(
            0,
            '{"result":"Claude response","modelUsage":{"model":{}}}',
            "",
        ),
        None,
    )

    assert extracted.output_extraction_status == "success"
    assert extracted.output_path is not None
    assert extracted.output_path.read_text(encoding="utf-8") == "Claude response"
    assert extracted.output_char_count == len("Claude response")
    extracted.output_path.unlink(missing_ok=True)


def test_claude_output_extractor_uses_stderr_when_stdout_is_empty() -> None:
    extracted = extract_claude_output(
        CommandResult(0, "", '{"result":"stderr response"}'),
        None,
    )

    assert extracted.output_extraction_status == "success"
    assert extracted.output_path is not None
    assert extracted.output_path.read_text(encoding="utf-8") == "stderr response"
    extracted.output_path.unlink(missing_ok=True)


def test_claude_output_extractor_uses_stderr_after_valid_missing_stdout() -> None:
    extracted = extract_claude_output(
        CommandResult(0, "{}", '{"result":"stderr response"}'),
        None,
    )

    assert extracted.output_extraction_status == "success"
    assert extracted.output_path is not None
    try:
        assert extracted.output_path.read_text(encoding="utf-8") == "stderr response"
    finally:
        extracted.output_path.unlink(missing_ok=True)


@pytest.mark.parametrize(
    ("stdout_text", "expected_status"),
    [
        ('{"result":', "malformed"),
        ('{"result":"   "}', "missing"),
    ],
)
def test_claude_output_extractor_does_not_fall_back_after_selected_stdout_result(
    stdout_text: str,
    expected_status: str,
) -> None:
    extracted = extract_claude_output(
        CommandResult(0, stdout_text, '{"result":"stderr response"}'),
        None,
    )

    assert extracted.output_extraction_status == expected_status
    assert extracted.output_path is None


@pytest.mark.parametrize(
    "stdout_text",
    [
        '{"result":"   "}',
        '{"result":123}',
        '{"result":"bad\\q"}',
        '{"result":"bad\\uZZZZ"}',
        '{"result":"accepted","metadata":invalid}',
        '{"result":"accepted","metadata":"bad\\q"}',
        '{"result":"accepted","metadata":{"ok":true "bad":false}}',
        '{"result":"accepted","metadata":[1 2]}',
        '{"result":"accepted","metadata":[1,]}',
        '{"result":"accepted","metadata":{"item":1,}}',
        '{"result":"accepted","metadata":{1:"bad"}}',
        '{"result":"unterminated',
        '{"result":"ok"} trailing',
        '{"result":"ok" "other":1}',
    ],
)
def test_claude_output_extractor_reports_missing_or_malformed_json(
    stdout_text: str,
) -> None:
    extracted = extract_claude_output(CommandResult(0, stdout_text, ""), None)

    expected_status = "missing" if "   " in stdout_text else "malformed"
    assert extracted.output_extraction_status == expected_status


def test_claude_output_extractor_decodes_escaped_and_nested_values() -> None:
    stdout_text = json.dumps(
        {
            "note": 'quote " slash \\ / back \b form \f line \n return '
            "\r tab \t unicode ☺",
            "nested": {"items": [{"text": "ignored"}]},
            "result": "emoji 😀",
        }
    )

    extracted = extract_claude_output(CommandResult(0, stdout_text, ""), None)

    assert extracted.output_extraction_status == "success"
    assert extracted.output_path is not None
    assert extracted.output_path.read_text(encoding="utf-8") == "emoji 😀"
    extracted.output_path.unlink(missing_ok=True)


def test_claude_output_extractor_uses_last_duplicate_result() -> None:
    extracted = extract_claude_output(
        CommandResult(0, '{"result":"first","result":"last"}', ""),
        None,
    )

    assert extracted.output_extraction_status == "success"
    assert extracted.output_path is not None
    try:
        assert extracted.output_path.read_text(encoding="utf-8") == "last"
        assert extracted.output_char_count == 4
    finally:
        extracted.output_path.unlink(missing_ok=True)


def test_claude_output_extractor_removes_owned_file_after_unexpected_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "owned-result.txt"

    def create_output_file() -> Path:
        output_path.touch()
        return output_path

    def raise_read_error(chunks: Iterable[str], selected_path: Path) -> int | None:
        assert list(chunks)
        assert selected_path == output_path
        raise OSError("read failed")

    monkeypatch.setattr(claude_json, "new_owned_output_file", create_output_file)
    monkeypatch.setattr(claude_json, "parse_claude_result", raise_read_error)

    with pytest.raises(OSError, match="read failed"):
        claude_json.extract_claude_output(
            CommandResult(0, '{"result":"ignored"}', ""),
            1,
        )

    assert not output_path.exists()


def test_claude_output_extractor_removes_owned_file_after_scan_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "owned-result.txt"

    def create_output_file() -> Path:
        output_path.touch()
        return output_path

    def raise_scan_error(selected_path: Path) -> bool:
        assert selected_path == output_path
        raise OSError("scan failed")

    monkeypatch.setattr(claude_json, "new_owned_output_file", create_output_file)
    monkeypatch.setattr(
        claude_json,
        "path_has_non_whitespace_text",
        raise_scan_error,
    )

    with pytest.raises(OSError, match="scan failed"):
        claude_json.extract_claude_output(
            CommandResult(0, '{"result":"ignored"}', ""),
            1,
        )

    assert not output_path.exists()


def test_claude_output_extractor_accepts_mixed_empty_nested_values() -> None:
    stdout_text = (
        '{"ignored":[{},[],{"items":[null,true,false,{"nested":[]}]}],"result":"ok"}'
    )

    extracted = extract_claude_output(CommandResult(0, stdout_text, ""), None)

    assert extracted.output_extraction_status == "success"
    assert extracted.output_path is not None
    try:
        assert extracted.output_path.read_text(encoding="utf-8") == "ok"
    finally:
        extracted.output_path.unlink(missing_ok=True)


def test_claude_output_extractor_accepts_deeply_nested_ignored_value() -> None:
    depth = 1_200
    stdout_text = '{"ignored":' + "[" * depth + "0" + "]" * depth + ',"result":"ok"}'

    extracted = extract_claude_output(CommandResult(0, stdout_text, ""), None)

    assert extracted.output_extraction_status == "success"
    assert extracted.output_path is not None
    assert extracted.owns_output_path
    try:
        assert extracted.output_path.read_text(encoding="utf-8") == "ok"
    finally:
        extracted.output_path.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "ignored_value",
    [
        "null",
        "true",
        "false",
        "0",
        "-0",
        "1234567890",
        "-12.5",
        "6.022e23",
        "1E-9",
    ],
)
def test_claude_output_extractor_accepts_valid_ignored_scalars(
    ignored_value: str,
) -> None:
    extracted = extract_claude_output(
        CommandResult(0, f'{{"ignored":{ignored_value},"result":"ok"}}', ""),
        None,
    )

    assert extracted.output_extraction_status == "success"
    assert extracted.output_path is not None
    extracted.output_path.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "ignored_value",
    ["Null", "tru", "falsehood", "+1", "01", "1.", ".1", "1e", "1e+"],
)
def test_claude_output_extractor_rejects_invalid_ignored_scalars(
    ignored_value: str,
) -> None:
    extracted = extract_claude_output(
        CommandResult(0, f'{{"result":"ok","ignored":{ignored_value}}}', ""),
        None,
    )

    assert extracted.output_extraction_status == "malformed"


def test_claude_output_extractor_streams_large_ignored_number() -> None:
    ignored_value = "1" * 1_000_000
    extracted = extract_claude_output(
        CommandResult(0, f'{{"ignored":{ignored_value},"result":"ok"}}', ""),
        None,
    )

    assert extracted.output_extraction_status == "success"
    assert extracted.output_path is not None
    extracted.output_path.unlink(missing_ok=True)


def test_claude_usage_parser_reports_missing_and_malformed_payloads() -> None:
    missing = machine_json.read_claude_model_usage(CommandResult(0, "", ""))
    malformed = machine_json.read_claude_model_usage(
        CommandResult(0, '{"modelUsage":{"model":[}}', "")
    )

    assert missing == (None, None)
    assert malformed[0] is None
    assert malformed[1]


def test_claude_usage_parser_uses_stderr_when_stdout_is_empty() -> None:
    usage, error = machine_json.read_claude_model_usage(
        CommandResult(
            0,
            "",
            '{"result":"stderr response","modelUsage":{"model":{"inputTokens":4}}}',
        )
    )

    assert error is None
    assert usage == {"model": {"inputTokens": 4}}


def test_claude_usage_parser_does_not_fall_back_after_valid_stdout() -> None:
    usage, error = machine_json.read_claude_model_usage(
        CommandResult(
            0,
            "{}",
            '{"modelUsage":{"model":{"inputTokens":4}}}',
        )
    )

    assert usage is None
    assert error is None


def test_claude_usage_parser_bounds_captured_model_usage(monkeypatch) -> None:
    monkeypatch.setattr(machine_json, "MAX_CAPTURED_CLAUDE_USAGE_BYTES", 1)

    usage, error = machine_json.read_claude_model_usage(
        CommandResult(0, '{"modelUsage":12}', "")
    )

    assert usage is None
    assert error


def test_claude_usage_parser_ignores_non_string_result() -> None:
    usage, error = machine_json.read_claude_model_usage(
        CommandResult(
            0,
            '{"result":123,"modelUsage":{"model":{"inputTokens":4}}}',
            "",
        )
    )

    assert error is None
    assert usage == {"model": {"inputTokens": 4}}


def test_claude_output_extractor_ignores_oversized_model_usage(monkeypatch) -> None:
    monkeypatch.setattr(machine_json, "MAX_CAPTURED_CLAUDE_USAGE_BYTES", 1)

    extracted = extract_claude_output(
        CommandResult(
            0,
            '{"result":"Claude response","modelUsage":{"model":{"inputTokens":123}}}',
            "",
        ),
        None,
    )

    assert extracted.output_extraction_status == "success"
    assert extracted.output_path is not None
    assert extracted.output_path.read_text(encoding="utf-8") == "Claude response"
    extracted.output_path.unlink(missing_ok=True)


def test_gemini_output_extractor_returns_top_level_response() -> None:
    extracted = extract_gemini_output(
        CommandResult(
            0,
            (FIXTURE_DIR / "gemini_stats.json").read_text(encoding="utf-8"),
            "",
        ),
        None,
    )

    assert extracted.output_extraction_status == "success"
    assert extracted.output_text == "Gemini response"


def test_kilo_output_extractor_preserves_completed_text_block_boundaries() -> None:
    extracted = extract_kilo_output(
        CommandResult(
            0,
            (FIXTURE_DIR / "kilo_events.jsonl").read_text(encoding="utf-8"),
            "",
        ),
        None,
    )

    assert extracted.output_extraction_status == "success"
    assert extracted.output_text == "Kilo\nresponse\n"


def test_machine_output_extractor_reports_malformed_and_missing_payloads() -> None:
    malformed = extract_gemini_output(CommandResult(0, "{bad", ""), None)
    missing = extract_gemini_output(CommandResult(0, '{"stats":{}}', ""), None)

    assert malformed.output_extraction_status == "malformed"
    assert missing.output_extraction_status == "missing"


@pytest.mark.parametrize(
    "stdout_text, expected_status",
    [
        ("", "missing"),
        ("[]", "malformed"),
        ('{"response":123}', "malformed"),
        ('{"response":"   "}', "missing"),
    ],
)
def test_gemini_output_extractor_validates_response_shape(
    stdout_text: str,
    expected_status: str,
) -> None:
    extracted = extract_gemini_output(CommandResult(0, stdout_text, ""), None)

    assert extracted.output_extraction_status == expected_status


@pytest.mark.parametrize("stdout_text", ["", "\n", '{"type":"step_finish"}'])
def test_kilo_output_extractor_reports_missing_text(stdout_text: str) -> None:
    extracted = extract_kilo_output(CommandResult(0, stdout_text, ""), None)

    assert extracted.output_extraction_status == "missing"


@pytest.mark.parametrize("stdout_text", ["not json", "[]"])
def test_kilo_output_extractor_reports_malformed_json_lines(stdout_text: str) -> None:
    extracted = extract_kilo_output(CommandResult(0, stdout_text, ""), None)

    assert extracted.output_extraction_status == "malformed"


def test_kilo_output_extractor_discards_partial_text_and_stops_on_malformed_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def events(result: CommandResult) -> Iterator[Mapping[str, object] | None]:
        assert result.returncode == 0
        yield {"type": "text", "text": "partial response"}
        yield None
        raise AssertionError("Read past the malformed event")

    monkeypatch.setattr(machine_json, "iter_stdout_json_objects", events)

    extracted = extract_kilo_output(CommandResult(0, "", ""), None)

    assert extracted.output_extraction_status == "malformed"
    assert extracted.output_text == ""
    assert extracted.output_path is None


def test_kilo_output_extractor_accepts_top_level_text_and_ignores_invalid_text_types() -> (
    None
):
    extracted = extract_kilo_output(
        CommandResult(
            0,
            "\n".join(
                [
                    '{"type":"text","text":"first"}',
                    '{"type":"text","part":{"text":7}}',
                    '{"type":"text","text":"second"}',
                ]
            ),
            "",
        ),
        None,
    )

    assert extracted.output_extraction_status == "success"
    assert extracted.output_text == "first\nsecond\n"


def test_kilo_output_extractor_trims_and_skips_blank_text_blocks() -> None:
    extracted = extract_kilo_output(
        CommandResult(
            0,
            "\n".join(
                [
                    '{"type":"text","part":{"text":" First sentence. "}}',
                    '{"type":"text","part":{"text":"  "}}',
                    '{"type":"text","part":{"text":" Second sentence. "}}',
                ]
            ),
            "",
        ),
        None,
    )

    assert extracted.output_extraction_status == "success"
    assert extracted.output_text == "First sentence.\nSecond sentence.\n"


def test_kilo_output_extractor_reassembles_json_lines_across_file_chunks(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "kilo-output.jsonl"
    long_text = "x" * 70_000
    output_path.write_text(
        json.dumps({"type": "text", "part": {"text": long_text}}) + "\n",
        encoding="utf-8",
    )

    extracted = extract_kilo_output(
        CommandResult(0, "", "", stdout_path=output_path),
        None,
    )

    assert extracted.output_extraction_status == "success"
    assert extracted.output_text == f"{long_text}\n"
