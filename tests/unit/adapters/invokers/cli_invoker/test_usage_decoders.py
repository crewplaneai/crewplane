from __future__ import annotations

import json
from pathlib import Path

import pytest

from crewplane.adapters.invokers.cli_invoker.machine_json import extract_gemini_output
from crewplane.adapters.invokers.cli_invoker.usage_decoders import (
    decode_claude_usage,
    decode_codex_usage,
    decode_gemini_usage,
    decode_kilo_usage,
)
from crewplane.architecture.contracts import (
    CommandResult,
    ProviderKind,
    ProviderTokenUsage,
)
from crewplane.core.config import AgentConfig, TokenPricing
from crewplane.runtime.agent.usage import InvocationUsageAccumulator

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "provider_usage"


def result_from_fixture(name: str) -> CommandResult:
    return CommandResult(
        returncode=0,
        stdout_text="",
        stderr_text="",
        stdout_path=FIXTURE_DIR / name,
    )


def test_codex_decoder_normalizes_terminal_usage_and_ignores_other_events() -> None:
    result = CommandResult(
        returncode=0,
        stdout_text=(
            '{"type":"response.completed","usage":{"input_tokens":99}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":12,"'
            'cached_input_tokens":8,"output_tokens":5,"reasoning_output_tokens":2}}\n'
        ),
        stderr_text=('{"type":"turn.completed","usage":{"input_tokens":1000}}\n'),
    )

    decoded = decode_codex_usage(result)

    assert decoded.error is None
    assert decoded.valid_report_count == 1
    assert decoded.tokens == ProviderTokenUsage(
        input=12,
        cached_input=8,
        output=5,
        reasoning=2,
        total=17,
    )


@pytest.mark.parametrize(
    ("stdout_text", "expected_error"),
    [
        ('{"type":"turn.completed","usage":null}', False),
        ('{"type":"turn.completed","usage":[]}', True),
        ('{"type":"turn.completed","usage":{}}', False),
        ("not json", False),
    ],
)
def test_codex_decoder_handles_missing_and_malformed_terminal_reports(
    stdout_text: str,
    expected_error: bool,
) -> None:
    decoded = decode_codex_usage(CommandResult(0, stdout_text, ""))

    assert bool(decoded.error) is expected_error
    if not expected_error:
        assert decoded.tokens is None


def test_claude_decoder_sums_model_usage_rows() -> None:
    decoded = decode_claude_usage(result_from_fixture("claude_model_usage.json"))

    assert decoded.error is None
    assert decoded.valid_report_count == 2
    assert decoded.tokens == ProviderTokenUsage(
        input=130,
        cached_input=27,
        cache_write=3,
        output=40,
        reasoning=None,
        total=170,
    )


def test_claude_decoder_preserves_partial_rows_and_rejects_bad_model_rows() -> None:
    partial = decode_claude_usage(
        CommandResult(
            0,
            '{"modelUsage":{"model":{"inputTokens":4}}}',
            "",
        )
    )
    malformed = decode_claude_usage(CommandResult(0, '{"modelUsage":{"model":[]}}', ""))

    assert partial.tokens == ProviderTokenUsage(input=4)
    assert malformed.tokens is None
    assert malformed.error


def test_claude_decoder_keeps_incomplete_aggregate_buckets_unknown() -> None:
    decoded = decode_claude_usage(
        CommandResult(
            0,
            '{"modelUsage":{"complete":{"inputTokens":10,"outputTokens":2},'
            '"partial":{"inputTokens":5}}}',
            "",
        )
    )

    assert decoded.valid_report_count == 2
    assert decoded.tokens == ProviderTokenUsage(input=15)


def test_claude_decoder_ignores_model_usage_without_counters() -> None:
    decoded = decode_claude_usage(CommandResult(0, '{"modelUsage":{"model":{}}}', ""))

    assert decoded.tokens is None
    assert decoded.error is None


def test_gemini_decoder_sums_model_rows_and_preserves_zero_values() -> None:
    decoded = decode_gemini_usage(result_from_fixture("gemini_stats.json"))

    assert decoded.error is None
    assert decoded.valid_report_count == 2
    assert decoded.tokens == ProviderTokenUsage(
        input=100,
        cached_input=20,
        cache_write=None,
        output=17,
        reasoning=5,
        total=117,
    )


def test_gemini_large_response_remains_extractable_and_decodable() -> None:
    response = "x" * 1_048_600
    stdout_text = json.dumps(
        {
            "response": response,
            "stats": {
                "models": [{"tokens": {"prompt": 12, "candidates": 7, "total": 19}}]
            },
        }
    )
    result = CommandResult(0, stdout_text, "")

    assert len(stdout_text.encode("utf-8")) > 1_048_576
    extracted = extract_gemini_output(result, None)
    decoded = decode_gemini_usage(result)

    assert extracted.output_extraction_status == "success"
    assert extracted.output_char_count == len(response)
    assert decoded.error is None
    assert decoded.tokens == ProviderTokenUsage(input=12, output=7, total=19)


@pytest.mark.parametrize(
    ("stdout_text", "expected_error"),
    [
        ('{"stats":{"models":{}}}', False),
        ('{"stats":{"models":{ "model": [] }}}', True),
        ('{"stats":{"models":[{"tokens":null}]}}', False),
        ('{"stats":{"models":[{"tokens":[]}]}}', True),
        ('{"stats":{"models":"bad"}}', True),
    ],
)
def test_gemini_decoder_handles_empty_and_malformed_model_stats(
    stdout_text: str,
    expected_error: bool,
) -> None:
    decoded = decode_gemini_usage(CommandResult(0, stdout_text, ""))

    assert bool(decoded.error) is expected_error
    if not expected_error:
        assert decoded.tokens is None


def test_kilo_decoder_sums_step_finish_records() -> None:
    decoded = decode_kilo_usage(result_from_fixture("kilo_events.jsonl"))

    assert decoded.error is None
    assert decoded.valid_report_count == 2
    assert decoded.tokens == ProviderTokenUsage(
        input=123,
        cached_input=20,
        cache_write=3,
        output=45,
        reasoning=5,
        total=168,
    )

    accumulator = InvocationUsageAccumulator(ProviderKind.KILO, prompt="prompt")
    accumulator.record_provider_usage(decoded)
    usage = accumulator.build_usage(
        config=AgentConfig(cli_cmd=["kilo"], provider_kind="kilo"),
        output_extraction_status="success",
    )

    assert usage.provider_usage_report_count == 2


@pytest.mark.parametrize(
    ("decoder", "payloads", "expected_tokens"),
    [
        pytest.param(
            decode_claude_usage,
            [
                {
                    "modelUsage": {
                        "valid": {"inputTokens": 10, "outputTokens": 2},
                        "invalid": {"inputTokens": "bad"},
                    }
                }
            ],
            ProviderTokenUsage(input=10, output=2, total=12),
            id="claude",
        ),
        pytest.param(
            decode_gemini_usage,
            [
                {
                    "stats": {
                        "models": [
                            {
                                "tokens": {
                                    "prompt": 10,
                                    "candidates": 2,
                                    "total": 12,
                                }
                            },
                            {"tokens": {"prompt": "bad"}},
                        ]
                    }
                }
            ],
            ProviderTokenUsage(input=10, output=2, total=12),
            id="gemini",
        ),
        pytest.param(
            decode_kilo_usage,
            [
                {
                    "type": "step_finish",
                    "part": {
                        "tokens": {
                            "input": 10,
                            "output": 4,
                            "reasoning": 1,
                            "cache": {"read": 2, "write": 1},
                        }
                    },
                },
                {
                    "type": "step_finish",
                    "part": {"tokens": {"input": "bad"}},
                },
            ],
            ProviderTokenUsage(
                input=13,
                cached_input=2,
                cache_write=1,
                output=5,
                reasoning=1,
                total=18,
            ),
            id="kilo",
        ),
    ],
)
def test_multi_report_decoders_retain_valid_usage_after_malformed_report(
    decoder,
    payloads: list[dict[str, object]],
    expected_tokens: ProviderTokenUsage,
) -> None:
    decoded = decoder(
        CommandResult(0, "\n".join(json.dumps(payload) for payload in payloads), "")
    )

    assert decoded.tokens == expected_tokens
    assert decoded.valid_report_count == 1
    assert decoded.error


def test_kilo_decoder_handles_missing_fields_and_bad_cache_payloads() -> None:
    partial = decode_kilo_usage(
        CommandResult(
            0,
            '{"type":"step_finish","part":{"tokens":{"input":4}}}',
            "",
        )
    )
    malformed = decode_kilo_usage(
        CommandResult(
            0,
            '{"type":"step_finish","part":{"tokens":{"cache":[]}}}',
            "",
        )
    )
    invalid_json = decode_kilo_usage(CommandResult(0, "not json", ""))

    assert partial.tokens == ProviderTokenUsage(input=4)
    assert malformed.tokens is None
    assert malformed.error
    assert invalid_json.tokens is None
    assert invalid_json.error


def test_kilo_decoder_ignores_events_without_token_parts() -> None:
    decoded = decode_kilo_usage(
        CommandResult(
            0,
            "\n".join(
                [
                    '{"type":"text","part":{"text":"response"}}',
                    '{"type":"step_finish","part":{}}',
                    '{"type":"step_finish"}',
                ]
            ),
            "",
        )
    )

    assert decoded.tokens is None
    assert decoded.error is None


@pytest.mark.parametrize(
    ("decoder", "payload", "expected"),
    [
        (
            decode_codex_usage,
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_output_tokens": 0,
                },
            },
            ProviderTokenUsage(
                input=0,
                cached_input=0,
                output=0,
                reasoning=0,
                total=0,
            ),
        ),
        (
            decode_claude_usage,
            {
                "modelUsage": {
                    "model": {
                        "inputTokens": 0,
                        "cacheReadInputTokens": 0,
                        "cacheCreationInputTokens": 0,
                        "outputTokens": 0,
                    }
                }
            },
            ProviderTokenUsage(
                input=0,
                cached_input=0,
                cache_write=0,
                output=0,
                total=0,
            ),
        ),
        (
            decode_gemini_usage,
            {
                "stats": {
                    "models": [
                        {
                            "tokens": {
                                "prompt": 0,
                                "cached": 0,
                                "candidates": 0,
                                "thoughts": 0,
                                "tool": 0,
                                "total": 0,
                            }
                        }
                    ]
                }
            },
            ProviderTokenUsage(
                input=0,
                cached_input=0,
                output=0,
                reasoning=0,
                total=0,
            ),
        ),
        (
            decode_kilo_usage,
            {
                "type": "step_finish",
                "part": {
                    "tokens": {
                        "input": 0,
                        "cache": {"read": 0, "write": 0},
                        "output": 0,
                        "reasoning": 0,
                    }
                },
            },
            ProviderTokenUsage(
                input=0,
                cached_input=0,
                cache_write=0,
                output=0,
                reasoning=0,
                total=0,
            ),
        ),
    ],
)
def test_all_zero_provider_report_remains_valid(decoder, payload, expected) -> None:
    decoded = decoder(CommandResult(0, json.dumps(payload), ""))

    assert decoded.error is None
    assert decoded.valid_report_count == 1
    assert decoded.tokens == expected


@pytest.mark.parametrize(
    ("decoder", "result"),
    [
        (
            decode_codex_usage,
            CommandResult(0, '{"type":"response.completed"}', ""),
        ),
        (
            decode_claude_usage,
            CommandResult(0, '{"result":"no usage"}', ""),
        ),
        (
            decode_gemini_usage,
            CommandResult(0, '{"response":"no stats"}', ""),
        ),
        (
            decode_kilo_usage,
            CommandResult(0, '{"type":"text","text":"no usage"}', ""),
        ),
    ],
)
def test_absent_provider_report_is_not_an_error(decoder, result) -> None:
    decoded = decoder(result)

    assert decoded.tokens is None
    assert decoded.error is None
    assert decoded.valid_report_count == 0


@pytest.mark.parametrize(
    ("decoder", "stdout_text"),
    [
        (
            decode_codex_usage,
            '{"type":"turn.completed","usage":{"input_tokens":"bad"}}',
        ),
        (
            decode_claude_usage,
            '{"modelUsage":{"model":{"inputTokens":"bad"}}}',
        ),
        (
            decode_gemini_usage,
            '{"stats":{"models":[{"tokens":{"prompt":"bad"}}]}}',
        ),
        (
            decode_kilo_usage,
            '{"type":"step_finish","part":{"tokens":{"input":"bad"}}}',
        ),
        (
            decode_codex_usage,
            '{"type":"turn.completed","usage":{"reasoning_output_tokens":true}}',
        ),
        (
            decode_claude_usage,
            '{"modelUsage":{"model":{"cacheCreationInputTokens":-1}}}',
        ),
        (
            decode_gemini_usage,
            '{"stats":{"models":[{"tokens":{"tool":1.5}}]}}',
        ),
        (
            decode_kilo_usage,
            '{"type":"step_finish","part":{"tokens":{"cache":{"write":"bad"}}}}',
        ),
    ],
)
def test_malformed_provider_report_returns_error_without_tokens(
    decoder,
    stdout_text: str,
) -> None:
    decoded = decoder(CommandResult(0, stdout_text, ""))

    assert decoded.tokens is None
    assert decoded.error
    assert decoded.valid_report_count == 0


def test_decoder_prefers_stdout_path_over_tail_text_and_never_reads_stderr() -> None:
    result = CommandResult(
        returncode=0,
        stdout_text='{"type":"turn.completed","usage":{"input_tokens":"bad"}}',
        stderr_text=('{"type":"turn.completed","usage":{"input_tokens":999}}'),
        stdout_path=FIXTURE_DIR / "codex_24_reports.jsonl",
    )

    decoded = decode_codex_usage(result)

    assert decoded.error is None
    assert decoded.tokens == ProviderTokenUsage(
        input=459384,
        cached_input=374416,
        output=9228,
        reasoning=16672,
        total=468612,
    )


def test_empty_stdout_path_falls_back_to_stdout_tail(tmp_path: Path) -> None:
    empty_path = tmp_path / "empty.jsonl"
    empty_path.write_text("", encoding="utf-8")
    result = CommandResult(
        returncode=0,
        stdout_text=(
            '{"type":"turn.completed","usage":{"input_tokens":2,"output_tokens":3}}'
        ),
        stderr_text="",
        stdout_path=empty_path,
    )

    decoded = decode_codex_usage(result)

    assert decoded.tokens == ProviderTokenUsage(input=2, output=3, total=5)


def test_twenty_four_codex_reports_accumulate_once_per_returned_report() -> None:
    accumulator = InvocationUsageAccumulator(ProviderKind.CODEX, prompt="prompt")
    for line in (FIXTURE_DIR / "codex_24_reports.jsonl").read_text().splitlines():
        accumulator.record_provider_usage(
            decode_codex_usage(CommandResult(0, line, ""))
        )

    usage = accumulator.build_usage(
        config=AgentConfig(cli_cmd=["codex"]),
        output_extraction_status="success",
    )

    assert usage.provider_usage_report_count == 24
    assert usage.provider_tokens == {
        "input": 7_359_384,
        "cached_input": 6_124_416,
        "cache_write": None,
        "output": 239_228,
        "reasoning": 108_672,
        "total": 7_598_612,
    }


def test_retry_reports_are_added_and_malformed_later_report_does_not_erase_them() -> (
    None
):
    accumulator = InvocationUsageAccumulator(ProviderKind.CODEX, prompt="prompt")
    accumulator.record_provider_usage(
        decode_codex_usage(
            CommandResult(
                0,
                '{"type":"turn.completed","usage":{"input_tokens":2,'
                '"output_tokens":3}}',
                "",
            )
        )
    )
    accumulator.record_provider_usage(
        decode_codex_usage(
            CommandResult(
                0,
                '{"type":"turn.completed","usage":{"input_tokens":4,'
                '"output_tokens":5}}',
                "",
            )
        )
    )
    accumulator.record_provider_usage(
        decode_codex_usage(
            CommandResult(
                0,
                '{"type":"turn.completed","usage":{"input_tokens":"bad"}}',
                "",
            )
        )
    )

    usage = accumulator.build_usage(
        config=AgentConfig(cli_cmd=["codex"]),
        output_extraction_status="success",
    )

    assert usage.provider_usage_report_count == 2
    assert usage.provider_usage_status == "full"
    assert usage.provider_tokens["input"] == 6
    assert usage.provider_tokens["output"] == 8
    assert usage.provider_tokens["total"] == 14


def test_retry_reports_keep_incomplete_aggregate_buckets_unknown() -> None:
    accumulator = InvocationUsageAccumulator(ProviderKind.CODEX, prompt="prompt")
    for stdout_text in (
        '{"type":"turn.completed","usage":{"input_tokens":2,"output_tokens":3}}',
        '{"type":"turn.completed","usage":{"input_tokens":4}}',
    ):
        accumulator.record_provider_usage(
            decode_codex_usage(CommandResult(0, stdout_text, ""))
        )

    usage = accumulator.build_usage(
        config=AgentConfig(
            cli_cmd=["codex"],
            pricing=TokenPricing(input=1_000_000, output=1_000_000),
        ),
        output_extraction_status="success",
    )

    assert usage.provider_usage_report_count == 2
    assert usage.provider_usage_status == "partial"
    assert usage.provider_tokens["input"] == 6
    assert usage.provider_tokens["output"] is None
    assert usage.provider_tokens["total"] is None
    assert usage.invocation_cost_confidence == "partial"
