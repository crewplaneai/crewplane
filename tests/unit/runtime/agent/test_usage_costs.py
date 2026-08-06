import pytest

from crewplane.architecture.contracts import (
    InvocationUsage,
    ProviderKind,
    ProviderTokenUsage,
)
from crewplane.core.config import AgentConfig, TokenPricing
from crewplane.runtime.agent.usage_costs import (
    classify_provider_usage_status,
    derive_configured_cost,
    estimate_token_count,
    provider_usage_buckets,
    roll_up_cost_confidence,
    visible_cost_token_count,
)


def agent_config(**pricing: float) -> AgentConfig:
    return AgentConfig(
        cli_cmd=["provider"],
        pricing=TokenPricing(**pricing),
    )


@pytest.mark.parametrize(
    ("char_count", "expected"),
    [
        pytest.param(-1, 0, id="negative"),
        pytest.param(0, 0, id="zero"),
        pytest.param(1, 1, id="one"),
        pytest.param(5, 2, id="round-up"),
    ],
)
def test_estimate_token_count_rounds_up(char_count: int, expected: int) -> None:
    assert estimate_token_count(char_count) == expected


def test_configured_cost_uses_provider_tokens_with_full_confidence() -> None:
    cost, confidence = derive_configured_cost(
        agent_config(input=2.0, output=4.0),
        ProviderTokenUsage(input=1_000_000, output=500_000),
        visible_input_tokens=1,
        visible_output_tokens=1,
        visible_estimate_tokens=2,
    )

    assert cost == pytest.approx(4.0)
    assert confidence == "full"


def test_configured_cost_does_not_reprice_inclusive_token_sub_buckets() -> None:
    cost, confidence = derive_configured_cost(
        agent_config(
            input=3.0,
            cached_input=0.3,
            cache_write=3.75,
            output=15.0,
            reasoning=5.0,
        ),
        ProviderTokenUsage(
            input=130,
            cached_input=20,
            cache_write=10,
            output=60,
            reasoning=10,
        ),
        visible_input_tokens=1,
        visible_output_tokens=1,
        visible_estimate_tokens=2,
    )

    assert cost == pytest.approx(0.0011435)
    assert confidence == "full"


def test_configured_cost_uses_fallback_when_priced_sub_bucket_is_unknown() -> None:
    cost, confidence = derive_configured_cost(
        agent_config(input=1_000_000.0, cached_input=1_000_000.0),
        ProviderTokenUsage(input=100),
        visible_input_tokens=2,
        visible_output_tokens=0,
        visible_estimate_tokens=2,
    )

    assert cost == pytest.approx(2.0)
    assert confidence == "partial"


def test_configured_cost_uses_visible_fallback_with_partial_confidence() -> None:
    cost, confidence = derive_configured_cost(
        agent_config(input=1_000_000.0, cached_input=1_000_000.0, output=1_000_000.0),
        ProviderTokenUsage(output=3),
        visible_input_tokens=2,
        visible_output_tokens=3,
        visible_estimate_tokens=5,
    )

    assert cost == pytest.approx(5.0)
    assert confidence == "partial"


def test_configured_cost_reports_none_without_pricing_or_usable_bucket() -> None:
    assert derive_configured_cost(
        agent_config(),
        ProviderTokenUsage(),
        1,
        1,
        2,
    ) == (None, "none")
    assert derive_configured_cost(
        agent_config(reasoning=1.0),
        ProviderTokenUsage(),
        1,
        1,
        2,
    ) == (None, "none")


@pytest.mark.parametrize(
    ("bucket", "expected"),
    [
        pytest.param("input", 2, id="input"),
        pytest.param("output", 3, id="output"),
        pytest.param("total", 5, id="total"),
        pytest.param("reasoning", None, id="unsupported"),
    ],
)
def test_visible_cost_token_count_maps_supported_buckets(
    bucket: str,
    expected: int | None,
) -> None:
    assert visible_cost_token_count(bucket, 2, 3, 5) == expected


def test_provider_usage_buckets_follow_pricing_shape() -> None:
    assert provider_usage_buckets(agent_config(total=1.0)) == ("total",)
    assert provider_usage_buckets(
        agent_config(input=1.0, cached_input=1.0, reasoning=1.0)
    ) == ("input", "output", "cached_input", "reasoning")


@pytest.mark.parametrize(
    ("provider", "tokens", "report_count", "error", "expected"),
    [
        pytest.param(
            ProviderKind.GENERIC,
            ProviderTokenUsage(input=1, output=1),
            1,
            None,
            "none",
            id="unstructured-provider",
        ),
        pytest.param(
            ProviderKind.CODEX,
            ProviderTokenUsage(),
            0,
            "bad",
            "malformed",
            id="malformed",
        ),
        pytest.param(
            ProviderKind.CODEX,
            ProviderTokenUsage(),
            0,
            None,
            "none",
            id="absent",
        ),
        pytest.param(
            ProviderKind.CLAUDE,
            ProviderTokenUsage(),
            1,
            None,
            "none",
            id="no-required-values",
        ),
        pytest.param(
            ProviderKind.CLAUDE,
            ProviderTokenUsage(input=1, output=2),
            1,
            None,
            "full",
            id="full",
        ),
        pytest.param(
            ProviderKind.CLAUDE,
            ProviderTokenUsage(input=1),
            1,
            None,
            "partial",
            id="partial",
        ),
    ],
)
def test_provider_usage_status_classification(
    provider: ProviderKind,
    tokens: ProviderTokenUsage,
    report_count: int,
    error: str | None,
    expected: str,
) -> None:
    assert (
        classify_provider_usage_status(
            agent_config(),
            provider,
            tokens,
            report_count,
            error,
        )
        == expected
    )


def invocation_usage(confidence: str) -> InvocationUsage:
    return InvocationUsage(
        attempt_count=1,
        cli_captured=True,
        output_extraction_status="success",
        provider_usage_status="none",
        provider_usage_report_count=0,
        provider_tokens={},
        visible_estimate_tokens=None,
        visible_estimate_method=None,
        visible_estimate_is_lower_bound=False,
        configured_cost_usd=None,
        invocation_cost_confidence=confidence,
        usage_parse_error=None,
    )


@pytest.mark.parametrize(
    ("confidences", "expected"),
    [
        pytest.param((), "none", id="empty"),
        pytest.param(("full", "full"), "full", id="full"),
        pytest.param(("none", "none"), "none", id="none"),
        pytest.param(("full", "partial"), "partial", id="partial"),
        pytest.param(("full", "none"), "mixed", id="mixed"),
    ],
)
def test_roll_up_cost_confidence(
    confidences: tuple[str, ...],
    expected: str,
) -> None:
    usages = tuple(invocation_usage(confidence) for confidence in confidences)

    assert roll_up_cost_confidence(usages) == expected
