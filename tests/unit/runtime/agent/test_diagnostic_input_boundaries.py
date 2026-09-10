from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from crewplane.architecture.contracts import ProviderKind
from crewplane.core.config import AgentConfig
from crewplane.runtime.agent.failures.evidence import collect_failure_evidence
from crewplane.runtime.agent.failures.formatting import fallback_summary
from crewplane.runtime.agent.failures.types import FailureSource
from crewplane.runtime.agent.quota.evidence import (
    collect_quota_context_lines,
    find_quota_evidence,
)
from crewplane.runtime.agent.quota.waits import extract_wait_candidates_from_line


@pytest.mark.parametrize(
    "line",
    [
        '{"type":"error", broken',
        '{"type":"progress","message":"working"}',
        '{"type":"error","error":{"count":1}}',
        '{"type":"error","error":"","message":null}',
        '{"error":false,"message":"ordinary progress"}',
        '{"type":"progress","message":"' + "x" * 128_000 + '"}',
    ],
    ids=[
        "malformed-json",
        "progress-record",
        "error-without-message",
        "empty-error",
        "false-error",
        "oversized-progress-record",
    ],
)
def test_invalid_or_nonfailure_json_does_not_become_failure_evidence(line: str) -> None:
    evidence, candidates, count = collect_failure_evidence(
        ProviderKind.GENERIC, [(line, "stdout_json")]
    )
    assert evidence == []
    assert candidates == [(line, "stdout_json")]
    assert count == 1


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"error": "  connection reset  "}, "connection reset"),
        ({"type": "request.failed", "message": "connection reset"}, "connection reset"),
        (
            {"type": "transport.error", "message": "connection reset"},
            "connection reset",
        ),
        (
            {
                "type": "turn.failed",
                "message": "connection reset",
                "padding": "x" * 128_000,
            },
            "connection reset",
        ),
        (
            {"is_error": True, "result": "connection reset", "padding": "x" * 128_000},
            "connection reset",
        ),
    ],
)
def test_failure_json_preserves_error_message_and_transport_classification(
    payload: dict[str, object], message: str
) -> None:
    evidence, candidates, count = collect_failure_evidence(
        ProviderKind.GENERIC, [(json.dumps(payload), "stderr_json")]
    )
    assert len(evidence) == 1
    assert evidence[0].summary.message == message
    assert evidence[0].summary.kind == "provider_transport_error"
    assert evidence[0].summary.source == "stderr_json"
    assert candidates == []
    assert count == 1


@pytest.mark.parametrize(
    ("lines", "expected", "condensed"),
    [
        ([], "No output captured.", False),
        (["yolo mode is enabled"], "yolo mode is enabled", False),
        (["useful detail", "loaded cached credentials"], "useful detail", True),
        (["request failed", "unrelated last line"], "request failed", True),
        (["at call()"], "at call()", False),
        (["x" * 250], "x" * 237 + "...", True),
    ],
    ids=[
        "empty",
        "notice-only",
        "ignore-noise",
        "prefer-error",
        "stack-only",
        "truncate",
    ],
)
def test_fallback_diagnostics_select_useful_bounded_message(
    lines: list[str], expected: str, condensed: bool
) -> None:
    summary = fallback_summary([(line, "stderr_text") for line in lines])
    assert summary.message == expected
    assert summary.source == ("stderr_text" if lines else "none")
    assert summary.condensed is condensed


@pytest.mark.parametrize("source", ["stdout_text", "stderr_text"])
def test_malformed_output_has_specific_failure_classification(
    source: FailureSource,
) -> None:
    evidence, _, _ = collect_failure_evidence(
        ProviderKind.GENERIC, [("invalid json output", source)]
    )
    assert len(evidence) == 1
    assert evidence[0].summary.kind == "malformed_provider_output"
    assert evidence[0].summary.phase == "provider_output"


@pytest.mark.parametrize(
    "line", ["Budget is depleted", "Budget-is-depleted", "BudgetIsDepleted"]
)
def test_configured_quota_hint_accepts_punctuation_variants(line: str) -> None:
    config = AgentConfig(
        cli_cmd=["local"], quota_reached_on_contains=["", "budget is depleted"]
    )
    assert find_quota_evidence([" ", line], "generic", config) == "budget is depleted"


def test_parser_specific_quota_hint_is_detected_without_custom_configuration() -> None:
    config = AgentConfig(cli_cmd=["local"])
    assert (
        find_quota_evidence(
            ["The server reports exhausted your capacity"], "gemini", config
        )
        == "exhausted your capacity"
    )
    assert (
        find_quota_evidence(["Provider says usage-limit-exceeded"], "codex", config)
        == "usage limit exceeded"
    )


def test_quota_context_retains_following_blank_and_custom_reset_details() -> None:
    config = AgentConfig(
        cli_cmd=["local"], quota_reached_on_contains=["budget depleted"]
    )
    lines = [
        "unrelated",
        "budget depleted",
        "",
        "unrelated again",
        "reset at tomorrow",
        "specific detail",
    ]
    assert collect_quota_context_lines(lines, "generic", config) == [
        "budget depleted",
        "",
        "reset at tomorrow",
        "specific detail",
    ]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("resetAt=2026-04-10T12:01:30", [90.0]),
        ("Reset at 2026-04-10T12:01:30Z", [90.0]),
        ("Reset at 2026-99-99T12:00:00Z", []),
        ("Reset at February 31, 2026 1 pm", []),
        ("Reset at 25 pm (America/New_York)", []),
    ],
)
def test_quota_timestamp_boundaries(line: str, expected: list[float]) -> None:
    now = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)
    assert extract_wait_candidates_from_line(line, now) == expected
