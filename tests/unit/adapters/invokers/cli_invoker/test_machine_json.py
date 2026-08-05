from __future__ import annotations

import json
from pathlib import Path

import pytest

from crewplane.adapters.invokers.cli_invoker import machine_json
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


@pytest.mark.parametrize(
    "stdout_text",
    [
        '{"result":"   "}',
        '{"result":123}',
        '{"result":"bad\\q"}',
        '{"result":"bad\\uZZZZ"}',
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


def test_claude_usage_parser_bounds_captured_model_usage(monkeypatch) -> None:
    monkeypatch.setattr(machine_json, "MAX_CAPTURED_USAGE_BYTES", 1)

    usage, error = machine_json.read_claude_model_usage(
        CommandResult(0, '{"modelUsage":{"model":{"inputTokens":1}}}', "")
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
    monkeypatch.setattr(machine_json, "MAX_CAPTURED_USAGE_BYTES", 1)

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
