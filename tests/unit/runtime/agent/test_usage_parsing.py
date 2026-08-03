import pytest

from crewplane.runtime.agent.usage_parsing import (
    find_usage_mapping,
    load_structured_output_payload,
    parse_provider_usage,
    parse_usage_mapping,
)


def test_claude_usage_parser_reads_stderr_payload() -> None:
    usage = parse_provider_usage(
        "claude",
        "",
        '{"usage":{"input_tokens":12,"output_tokens":4}}',
    )

    assert usage.status == "parsed"
    assert usage.tokens is not None
    assert usage.tokens.input == 12
    assert usage.tokens.output == 4


@pytest.mark.parametrize(
    ("stdout", "expected_status", "expected_error"),
    [
        pytest.param("", "none", None, id="empty"),
        pytest.param("{}", "none", None, id="missing-usage"),
        pytest.param("{bad", "malformed", "Malformed structured output", id="json"),
        pytest.param("[]", "malformed", "expected a JSON object", id="non-object"),
        pytest.param(
            '{"usage":{"input_tokens":true}}',
            "malformed",
            "input must be a non-negative integer",
            id="invalid-usage",
        ),
    ],
)
def test_claude_usage_parser_classifies_absent_and_malformed_payloads(
    stdout: str,
    expected_status: str,
    expected_error: str | None,
) -> None:
    usage = parse_provider_usage("claude", stdout, "")

    assert usage.status == expected_status
    if expected_error is None:
        assert usage.error is None
    else:
        assert usage.error is not None
        assert expected_error in usage.error


def test_unknown_usage_parser_reports_no_usage() -> None:
    assert parse_provider_usage("none", "anything", "anything").status == "none"


def test_codex_usage_parser_uses_latest_valid_candidate() -> None:
    usage = parse_provider_usage(
        "codex",
        "\n".join(
            [
                "not json",
                '{"usage":{"input_tokens":"bad"}}',
                '{"response":{"usage":{"input_tokens":7}}}',
                '{"events":[{"usage":{"input_tokens":9,"output_tokens":2}}]}',
            ]
        ),
        "",
    )

    assert usage.status == "parsed"
    assert usage.tokens is not None
    assert usage.tokens.input == 9


def test_codex_usage_parser_preserves_malformed_candidate_when_no_valid_one() -> None:
    usage = parse_provider_usage(
        "codex",
        '{"usage":{"output_tokens":-1}}',
        "",
    )

    assert usage.status == "malformed"
    assert usage.error is not None
    assert "output must be a non-negative integer" in usage.error


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param('{"usage":{}}', id="empty-usage"),
        pytest.param('{"message":"done"}', id="no-usage"),
        pytest.param("\n", id="blank"),
    ],
)
def test_codex_usage_parser_reports_none_without_token_values(payload: str) -> None:
    assert parse_provider_usage("codex", payload, "").status == "none"


def test_structured_payload_loader_uses_first_nonblank_line() -> None:
    payload, error = load_structured_output_payload(
        "\n",
        '\n{"result":"done"}\n{"ignored":true}',
    )

    assert payload == {"result": "done"}
    assert error is None


def test_find_usage_mapping_walks_nested_objects_and_lists() -> None:
    payload = {"outer": [{"ignored": 1}, {"response": {"usage": {"total": 8}}}]}

    assert find_usage_mapping(payload) == {"total": 8}
    assert find_usage_mapping("not a container") is None


def test_usage_mapping_accepts_aliases_and_integral_floats() -> None:
    usage = parse_usage_mapping(
        {
            "prompt_tokens": 10.0,
            "cache_read_input_tokens": 3,
            "cache_write_tokens": 2,
            "completion_tokens": 4,
            "reasoning_tokens": 1,
            "total": 20,
        }
    )

    assert usage.status == "parsed"
    assert usage.tokens is not None
    assert usage.tokens.as_dict() == {
        "input": 10,
        "cached_input": 3,
        "cache_write": 2,
        "output": 4,
        "reasoning": 1,
        "total": 20,
    }


@pytest.mark.parametrize(
    ("payload", "error_fragment"),
    [
        pytest.param([], "expected an object", id="non-object"),
        pytest.param({"input_tokens": True}, "input must", id="boolean"),
        pytest.param({"input_tokens": -1}, "input must", id="negative-integer"),
        pytest.param({"input_tokens": -1.0}, "input must", id="negative-float"),
        pytest.param({"input_tokens": 1.5}, "input must", id="fractional-float"),
        pytest.param({"input_tokens": "1"}, "input must", id="string"),
    ],
)
def test_usage_mapping_rejects_invalid_bucket_values(
    payload: object,
    error_fragment: str,
) -> None:
    usage = parse_usage_mapping(payload)

    assert usage.status == "malformed"
    assert usage.error is not None
    assert error_fragment in usage.error


def test_usage_mapping_reports_none_when_no_known_bucket_exists() -> None:
    assert parse_usage_mapping({"requests": 2}).status == "none"
