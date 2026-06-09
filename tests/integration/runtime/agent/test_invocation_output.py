from orchestrator_cli.architecture.contracts import CommandResult
from orchestrator_cli.core.config import AgentConfig
from orchestrator_cli.runtime.agent.invocation.output import (
    cleanup_extracted_invocation_output,
    extract_invocation_output,
    write_extracted_invocation_output,
)
from orchestrator_cli.runtime.agent.usage import (
    build_fallback_usage_from_output_file,
    estimate_token_count,
    output_text_for_usage,
)
from orchestrator_cli.runtime.agent.usage_parsing import (
    parse_provider_usage_from_result,
)


def test_extract_codex_output_reads_structured_file_and_usage(tmp_path) -> None:
    output_path = tmp_path / "structured-output.txt"
    output_path.write_text("final answer", encoding="utf-8")
    result = CommandResult(
        returncode=0,
        stdout_text=(
            '{"type":"response.completed","response":'
            '{"usage":{"input_tokens":10,"output_tokens":2}}}'
        ),
        stderr_text="",
    )

    extracted = extract_invocation_output(
        output_extraction_mode="codex_last_message_file",
        usage_parser="codex",
        cmd=["codex", "exec"],
        result=result,
        structured_output_file=output_path,
    )

    assert extracted.output_text == ""
    assert extracted.output_path == output_path
    assert extracted.output_char_count == len("final answer")
    assert extracted.output_extraction_status == "success"
    assert extracted.parsed_provider_usage.status == "parsed"
    assert extracted.parsed_provider_usage.tokens is not None
    assert extracted.parsed_provider_usage.tokens.input == 10


def test_extract_codex_output_keeps_usage_parse_failure_telemetry_only(
    tmp_path,
) -> None:
    output_path = tmp_path / "last-message.txt"
    output_path.write_text("final answer", encoding="utf-8")
    result = CommandResult(
        returncode=0,
        stdout_text='{"usage":{"input_tokens":"bad"}}',
        stderr_text="",
    )

    extracted = extract_invocation_output(
        output_extraction_mode="codex_last_message_file",
        usage_parser="codex",
        cmd=["codex", "exec"],
        result=result,
        structured_output_file=output_path,
    )

    assert extracted.output_text == ""
    assert extracted.output_path == output_path
    assert extracted.output_char_count == len("final answer")
    assert extracted.output_extraction_status == "success"
    assert extracted.parsed_provider_usage.status == "malformed"


def test_extract_claude_output_streams_result_to_owned_file(tmp_path) -> None:
    stream_path = tmp_path / "claude-stdout.json"
    stream_path.write_text(
        '{"result":"final answer","usage":{"input_tokens":12,"output_tokens":4}}',
        encoding="utf-8",
    )
    result = CommandResult(
        returncode=0,
        stdout_text="",
        stderr_text="",
        stdout_path=stream_path,
    )

    extracted = extract_invocation_output(
        output_extraction_mode="claude_json",
        usage_parser="claude",
        cmd=["claude"],
        result=result,
        structured_output_file=None,
    )

    assert extracted.output_text == ""
    assert extracted.output_path is not None
    assert extracted.output_path != stream_path
    assert extracted.owns_output_path
    assert extracted.output_char_count == len("final answer")
    assert extracted.output_path.read_text(encoding="utf-8") == "final answer"
    assert extracted.output_extraction_status == "success"
    assert extracted.parsed_provider_usage.status == "parsed"
    assert extracted.parsed_provider_usage.tokens is not None
    assert extracted.parsed_provider_usage.tokens.input == 12

    output_file = tmp_path / "final.md"
    write_extracted_invocation_output(extracted, output_file)
    assert output_file.read_text(encoding="utf-8") == "final answer"
    cleanup_extracted_invocation_output(extracted)
    assert not extracted.output_path.exists()


def test_extract_visible_output_returns_stderr_fallback_notice() -> None:
    extracted = extract_invocation_output(
        output_extraction_mode="visible",
        usage_parser="none",
        cmd=["tool"],
        result=CommandResult(
            returncode=0,
            stdout_text="",
            stderr_text="payload",
        ),
        structured_output_file=None,
    )

    assert extracted.output_text == "payload"
    assert extracted.output_extraction_status == "success"
    assert extracted.notice is not None
    assert extracted.notice.operation == "stderr_fallback"


def test_extract_visible_output_uses_persisted_stdout_without_materializing(
    tmp_path,
) -> None:
    stream_path = tmp_path / "stdout.txt"
    stream_path.write_text("line 1\nline 2", encoding="utf-8")

    extracted = extract_invocation_output(
        output_extraction_mode="visible",
        usage_parser="none",
        cmd=["tool"],
        result=CommandResult(
            returncode=0,
            stdout_text="line 2",
            stderr_text="",
            stdout_path=stream_path,
        ),
        structured_output_file=None,
    )

    assert extracted.output_text == ""
    assert extracted.output_path == stream_path
    assert extracted.output_char_count == len("line 1\nline 2")

    output_file = tmp_path / "final.md"
    write_extracted_invocation_output(extracted, output_file)
    assert output_file.read_text(encoding="utf-8") == "line 1\nline 2"


def test_extract_claude_output_reports_malformed_json() -> None:
    extracted = extract_invocation_output(
        output_extraction_mode="claude_json",
        usage_parser="claude",
        cmd=["claude"],
        result=CommandResult(returncode=0, stdout_text="{bad", stderr_text=""),
        structured_output_file=None,
    )

    assert extracted.output_text == ""
    assert extracted.output_extraction_status == "malformed"


def test_extract_claude_output_reports_missing_result_for_empty_object() -> None:
    extracted = extract_invocation_output(
        output_extraction_mode="claude_json",
        usage_parser="claude",
        cmd=["claude"],
        result=CommandResult(returncode=0, stdout_text="{ }", stderr_text=""),
        structured_output_file=None,
    )

    assert extracted.output_text == ""
    assert extracted.output_extraction_status == "missing"


def test_parse_provider_usage_from_result_reads_persisted_stdout_tail(tmp_path) -> None:
    output_path = tmp_path / "usage-stream.txt"
    payload = '{"type":"response.completed","response":{"usage":{"input_tokens":120,"output_tokens":30}}}'
    repeated = "\n".join(["noise"] * 500)
    output_path.write_text(f"{repeated}\n{payload}", encoding="utf-8")
    usage = parse_provider_usage_from_result(
        "codex",
        CommandResult(
            returncode=0,
            stdout_text="",
            stderr_text="",
            stdout_path=output_path,
        ),
    )
    assert usage.status == "parsed"
    assert usage.tokens is not None
    assert usage.tokens.input == 120
    assert usage.tokens.output == 30


def test_output_text_for_usage_preserves_in_memory_stream_newlines() -> None:
    output_text = output_text_for_usage(
        CommandResult(returncode=0, stdout_text="done", stderr_text="")
    )

    assert output_text == "done"


def test_output_text_for_usage_preserves_persisted_stream_newlines(tmp_path) -> None:
    output_path = tmp_path / "stdout.txt"
    output_path.write_text("done", encoding="utf-8")

    output_text = output_text_for_usage(
        CommandResult(
            returncode=0,
            stdout_text="",
            stderr_text="",
            stdout_path=output_path,
        )
    )

    assert output_text == "done"


def test_build_fallback_usage_from_output_file_streams_visible_estimate(
    tmp_path,
    monkeypatch,
) -> None:
    output_path = tmp_path / "large-output.txt"
    output_text = "visible output\n" * 1000
    output_path.write_text(output_text, encoding="utf-8")

    def fail_read_text(self, *args, **kwargs):  # noqa: ARG001
        raise AssertionError(
            "fallback usage must not materialize output with read_text"
        )

    monkeypatch.setattr(type(output_path), "read_text", fail_read_text)

    usage = build_fallback_usage_from_output_file(
        prompt="prompt",
        output_file=output_path,
        config=_agent_config(),
    )

    assert usage.visible_estimate_tokens == (
        estimate_token_count(len("prompt")) + estimate_token_count(len(output_text))
    )


def _agent_config():
    return AgentConfig(cli_cmd=["tool"], default_model="test")
