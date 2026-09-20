from copy import deepcopy

import pytest

from crewplane.adapters.invokers.cli_invoker.providers.opencode_events import (
    decode_opencode_usage,
)
from crewplane.architecture.contracts import CommandResult, ProviderTokenUsage
from tests.helpers.opencode import (
    finish_event,
    fixture_text,
    native_tokens,
    stream,
    text_event,
)


def decode(*events):
    return decode_opencode_usage(CommandResult(0, stream(*events), ""))


@pytest.mark.parametrize("total,expected", [(None, 25), (99, 99), (0, 0)])
def test_disjoint_native_counters_and_reported_total_precedence(total, expected):
    payload = native_tokens(**({"total": total} if total is not None else {}))
    result = decode(finish_event(tokens=payload, cost=123.45))
    assert result.tokens == ProviderTokenUsage(
        input=16, cached_input=4, cache_write=2, output=9, reasoning=3, total=expected
    )
    assert result.valid_report_count == 1
    assert result.error is None


def test_distinct_steps_are_aggregated_once_with_exact_unknown_semantics():
    first = finish_event("tool-calls", tokens=native_tokens())
    second = finish_event(
        part_id="finish-2",
        message="message-2",
        tokens=native_tokens(input=None, total=100),
    )
    result = decode(first, deepcopy(first), second, deepcopy(second))
    assert result.valid_report_count == 2
    assert result.tokens == ProviderTokenUsage(
        input=None, cached_input=8, cache_write=4, output=18, reasoning=6, total=125
    )


@pytest.mark.parametrize(
    "fixture,reports,total", [("simple", 1, 25), ("multistep", 2, 50)]
)
def test_synthetic_fixture_reports(fixture, reports, total):
    result = decode_opencode_usage(CommandResult(0, fixture_text(fixture), ""))
    assert result.valid_report_count == reports
    assert result.tokens.total == total


def test_zero_is_a_valid_counter_and_missing_counts_stay_unknown():
    zero = native_tokens(input=0, output=0, reasoning=0, cache={"read": 0, "write": 0})
    result = decode(finish_event(tokens=zero))
    assert result.valid_report_count == 1
    assert set(result.tokens.as_dict().values()) == {0}
    partial = decode(
        finish_event(tokens={"input": 10, "output": 6, "cache": {"read": 4}})
    )
    assert partial.tokens == ProviderTokenUsage(cached_input=4)
    assert partial.valid_report_count == 1
    assert partial.error is None


@pytest.mark.parametrize("tokens", [{}, {"input": 1}, {"cache": None}, {"input": None}])
def test_no_known_normalized_count_yields_no_report(tokens):
    result = decode(
        text_event(), finish_event(), finish_event(part_id="second", tokens=tokens)
    )
    assert result.tokens is None
    assert result.valid_report_count == 0
    assert result.error is None


@pytest.mark.parametrize(
    "key", ["input", "output", "reasoning", "total", "read", "write"]
)
@pytest.mark.parametrize("value", [True, False, -1, 1.5, "12", [], float("nan")])
def test_invalid_counters_record_errors_without_discarding_valid_steps(key, value):
    payload = native_tokens()
    (payload["cache"] if key in {"read", "write"} else payload)[key] = value
    result = decode(
        finish_event(tokens=native_tokens()),
        finish_event(part_id="bad", tokens=payload),
        finish_event(part_id="third", tokens=native_tokens()),
    )
    assert result.error is not None
    assert result.valid_report_count == 2
    assert result.tokens.total == 50


@pytest.mark.parametrize(
    "payload", [None, [], "tokens", {"cache": []}, {"cache": "invalid"}]
)
def test_malformed_usage_shapes_are_reported(payload):
    result = decode(finish_event(tokens=payload))
    assert result.error is not None
    assert result.tokens is None


def test_contradictory_duplicate_reports_keep_the_first_report():
    original = finish_event(tokens=native_tokens())
    duplicate = finish_event(tokens=native_tokens(total=80))
    result = decode(original, duplicate, original)
    assert result.valid_report_count == 1
    assert result.tokens.total == 25
    assert "Contradictory" in result.error


def test_duplicate_comparison_preserves_native_disjoint_counts():
    original = finish_event(tokens=native_tokens(reasoning=None))
    changed = finish_event(tokens=native_tokens(reasoning=None, output=100))
    result = decode(original, changed)
    assert result.valid_report_count == 1
    assert result.error is not None


def test_usage_is_independent_of_answer_completion_errors_and_stderr():
    result = decode_opencode_usage(
        CommandResult(
            1,
            stream({"type": "error"}, finish_event("length", tokens=native_tokens())),
            stream(finish_event(part_id="stderr", tokens=native_tokens())),
        )
    )
    assert result.valid_report_count == 1
    assert result.tokens.total == 25
    assert result.error is None


@pytest.mark.parametrize(
    "malformed",
    ["not json", "[]", '{"type": "step_finish", "part": {"tokens": {"total": 1}}}'],
)
def test_valid_reports_survive_malformed_records_before_and_after(malformed):
    valid = stream(finish_event(tokens=native_tokens()))
    result = decode_opencode_usage(
        CommandResult(0, malformed + "\n" + valid + malformed, "")
    )
    assert result.valid_report_count == 1
    assert result.tokens.total == 25
    assert result.error is not None


def test_report_identity_must_match_envelope():
    invalid = finish_event(tokens=native_tokens())
    invalid["sessionID"] = "different"
    assert decode(invalid).error is not None


@pytest.mark.parametrize("contents,reports", [(fixture_text(), 1), (" \n", 0)])
def test_usage_reads_borrowed_capture_instead_of_inline_tail(
    tmp_path, contents, reports
):
    capture = tmp_path / "stdout.jsonl"
    capture.write_text(contents)
    result = decode_opencode_usage(
        CommandResult(0, "invalid tail", "", stdout_path=capture)
    )
    assert result.valid_report_count == reports
    assert result.error is None
    assert capture.read_text() == contents
    assert list(tmp_path.iterdir()) == [capture]
