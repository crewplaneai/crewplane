from pathlib import Path

import pytest

from crewplane.architecture.contracts import CommandResult
from crewplane.runtime.agent.invocation.claude_json import (
    extract_claude_json_result,
)


def assert_successful_output(result: CommandResult, expected: str) -> None:
    extracted = extract_claude_json_result(result)
    try:
        assert extracted.output_extraction_status == "success"
        assert extracted.output_path is not None
        with extracted.output_path.open("r", encoding="utf-8", newline="") as handle:
            assert handle.read() == expected
        assert extracted.output_char_count == len(expected)
        assert extracted.owns_output_path
    finally:
        if extracted.output_path is not None:
            extracted.output_path.unlink(missing_ok=True)


def test_extracts_stderr_when_stdout_is_missing() -> None:
    assert_successful_output(
        CommandResult(
            returncode=0,
            stdout_text="",
            stderr_text='{"result":"stderr answer"}',
        ),
        "stderr answer",
    )


def test_whitespace_stdout_file_falls_back_to_stderr(tmp_path: Path) -> None:
    stdout_path = tmp_path / "stdout.json"
    stdout_path.write_text(" \n\t", encoding="utf-8")

    assert_successful_output(
        CommandResult(
            returncode=0,
            stdout_text="ignored fallback",
            stderr_text='{"result":"stderr answer"}',
            stdout_path=stdout_path,
        ),
        "stderr answer",
    )


def test_missing_stdout_path_uses_in_memory_fallback(tmp_path: Path) -> None:
    assert_successful_output(
        CommandResult(
            returncode=0,
            stdout_text='{"result":"memory answer"}',
            stderr_text="",
            stdout_path=tmp_path / "does-not-exist",
        ),
        "memory answer",
    )


def test_streaming_parser_decodes_escapes_and_skips_unknown_values() -> None:
    payload = (
        '{"metadata":{"nested":[1,true,null,{"text":"\\u263a"}]},'
        '"result":"line\\nquote: \\" slash: \\/ tab:\\t '
        'emoji:\\uD83D\\uDE00","after":[false]}'
    )

    assert_successful_output(
        CommandResult(returncode=0, stdout_text=payload, stderr_text=""),
        'line\nquote: " slash: / tab:\t emoji:😀',
    )


@pytest.mark.parametrize(
    ("escaped", "expected"),
    [
        pytest.param("\\b\\f\\r", "\b\f\r", id="control-escapes"),
        pytest.param("\\uD800x", "�x", id="unpaired-high-surrogate"),
        pytest.param("\\uDC00", "�", id="unpaired-low-surrogate"),
        pytest.param("\\u0041", "A", id="basic-unicode"),
    ],
)
def test_result_string_handles_json_escape_forms(escaped: str, expected: str) -> None:
    assert_successful_output(
        CommandResult(
            returncode=0,
            stdout_text=f'{{"result":"{escaped}"}}',
            stderr_text="",
        ),
        expected,
    )


def test_file_parser_handles_values_split_across_read_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream_path = tmp_path / "claude.json"
    stream_path.write_text(
        '{"usage":{"input_tokens":3},"result":"chunked 😀 answer"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "crewplane.runtime.agent.invocation.claude_json.STREAM_READ_BYTES",
        1,
    )

    extracted = extract_claude_json_result(
        CommandResult(
            returncode=0,
            stdout_text="",
            stderr_text="",
            stdout_path=stream_path,
        )
    )
    try:
        assert extracted.output_extraction_status == "success"
        assert extracted.output_path is not None
        assert extracted.output_path.read_text(encoding="utf-8") == "chunked 😀 answer"
        assert extracted.parsed_provider_usage.status == "parsed"
        assert extracted.parsed_provider_usage.tokens is not None
        assert extracted.parsed_provider_usage.tokens.input == 3
    finally:
        if extracted.output_path is not None:
            extracted.output_path.unlink(missing_ok=True)


def test_usage_without_result_does_not_replace_missing_output() -> None:
    extracted = extract_claude_json_result(
        CommandResult(
            returncode=0,
            stdout_text='{"usage":{"total_tokens":42}}',
            stderr_text="",
        )
    )

    assert extracted.output_extraction_status == "missing"
    assert extracted.output_path is None
    assert extracted.parsed_provider_usage.status == "none"


def test_invalid_usage_does_not_discard_valid_result() -> None:
    extracted = extract_claude_json_result(
        CommandResult(
            returncode=0,
            stdout_text='{"usage":{"input_tokens":"bad"},"result":"answer"}',
            stderr_text="",
        )
    )
    try:
        assert extracted.output_extraction_status == "success"
        assert extracted.parsed_provider_usage.status == "malformed"
        assert extracted.parsed_provider_usage.error is not None
        assert "input must be a non-negative integer" in (
            extracted.parsed_provider_usage.error
        )
    finally:
        if extracted.output_path is not None:
            extracted.output_path.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("[]", id="not-object"),
        pytest.param("{", id="truncated-object"),
        pytest.param('{"result":null}', id="non-string-result"),
        pytest.param('{"result":"answer"]', id="invalid-member-separator"),
        pytest.param('{"result":"answer"} trailing', id="trailing-data"),
        pytest.param('{"result":"\\x"}', id="invalid-string-escape"),
        pytest.param('{"result":"\\uZZZZ"}', id="invalid-unicode-escape"),
        pytest.param('{"ignored":[1,2}', id="unterminated-array"),
        pytest.param('{"ignored":', id="missing-value"),
    ],
)
def test_malformed_payloads_are_reported_without_owned_file(payload: str) -> None:
    extracted = extract_claude_json_result(
        CommandResult(returncode=0, stdout_text=payload, stderr_text="")
    )

    assert extracted.output_extraction_status == "malformed"
    assert extracted.output_path is None
    assert not extracted.owns_output_path


def test_empty_streams_report_missing_output() -> None:
    extracted = extract_claude_json_result(
        CommandResult(returncode=0, stdout_text=" \n", stderr_text="\t")
    )

    assert extracted.output_extraction_status == "missing"
    assert extracted.parsed_provider_usage.status == "none"
