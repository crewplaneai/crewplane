from orchestrator_cli.runtime.agent.invocation.output import extract_invocation_output
from orchestrator_cli.runtime.agent.types import CommandResult


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
        provider_kind="codex",
        cmd=["codex", "exec"],
        result=result,
        structured_output_file=output_path,
    )

    assert extracted.output_text == "final answer"
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
        provider_kind="codex",
        cmd=["codex", "exec"],
        result=result,
        structured_output_file=output_path,
    )

    assert extracted.output_text == "final answer"
    assert extracted.output_extraction_status == "success"
    assert extracted.parsed_provider_usage.status == "malformed"


def test_extract_visible_output_returns_stderr_fallback_notice() -> None:
    extracted = extract_invocation_output(
        provider_kind="generic",
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


def test_extract_claude_output_reports_malformed_json() -> None:
    extracted = extract_invocation_output(
        provider_kind="claude",
        cmd=["claude"],
        result=CommandResult(returncode=0, stdout_text="{bad", stderr_text=""),
        structured_output_file=None,
    )

    assert extracted.output_text == ""
    assert extracted.output_extraction_status == "malformed"
