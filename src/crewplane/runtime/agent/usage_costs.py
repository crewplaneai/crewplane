from __future__ import annotations

from math import ceil

from crewplane.architecture.contracts import (
    AggregateCostConfidence,
    InvocationCostConfidence,
    ProviderKind,
    ProviderUsageStatus,
)
from crewplane.core.config import AgentConfig

from .usage_types import (
    STRUCTURED_PROVIDER_KINDS,
    InvocationUsage,
    ParsedProviderUsage,
    ProviderTokenUsage,
    TokenBucket,
)

TOKENS_PER_MILLION = 1_000_000


def estimate_token_count(char_count: int) -> int:
    if char_count <= 0:
        return 0
    return ceil(char_count / 4)


def derive_configured_cost(
    config: AgentConfig,
    provider_tokens: ProviderTokenUsage,
    visible_input_tokens: int,
    visible_output_tokens: int,
    visible_estimate_tokens: int,
) -> tuple[float | None, InvocationCostConfidence]:
    pricing = config.pricing.as_dict()
    configured_buckets = [
        bucket for bucket, rate in pricing.items() if rate is not None
    ]
    if not configured_buckets:
        return None, "none"

    total_cost = 0.0
    computed_bucket_count = 0
    missing_bucket_count = 0
    fallback_bucket_count = 0
    provider_token_map = provider_tokens.as_dict()
    for bucket in configured_buckets:
        token_count = provider_token_map[bucket]
        if token_count is None:
            token_count = visible_cost_token_count(
                bucket=bucket,
                visible_input_tokens=visible_input_tokens,
                visible_output_tokens=visible_output_tokens,
                visible_estimate_tokens=visible_estimate_tokens,
            )
            if token_count is not None:
                fallback_bucket_count += 1
        if token_count is None:
            missing_bucket_count += 1
            continue
        rate = pricing[bucket]
        if rate is None:
            continue
        total_cost += token_count * rate / TOKENS_PER_MILLION
        computed_bucket_count += 1

    if computed_bucket_count == 0:
        return None, "none"
    if missing_bucket_count == 0 and fallback_bucket_count == 0:
        return total_cost, "full"
    return total_cost, "partial"


def visible_cost_token_count(
    bucket: str,
    visible_input_tokens: int,
    visible_output_tokens: int,
    visible_estimate_tokens: int,
) -> int | None:
    if bucket == "input":
        return visible_input_tokens
    if bucket == "output":
        return visible_output_tokens
    if bucket == "total":
        return visible_estimate_tokens
    return None


def provider_usage_buckets(config: AgentConfig) -> tuple[TokenBucket, ...]:
    pricing_buckets = config.pricing.configured_buckets()
    if "total" in pricing_buckets:
        return ("total",)
    required_buckets: list[TokenBucket] = ["input", "output"]
    optional_buckets: tuple[TokenBucket, ...] = (
        "cached_input",
        "cache_write",
        "reasoning",
    )
    for bucket in optional_buckets:
        if bucket in pricing_buckets:
            required_buckets.append(bucket)
    return tuple(required_buckets)


def classify_provider_usage_status(
    config: AgentConfig,
    provider_kind: ProviderKind,
    parsed_usage: ParsedProviderUsage,
) -> ProviderUsageStatus:
    if provider_kind not in STRUCTURED_PROVIDER_KINDS:
        return "none"
    if parsed_usage.status == "malformed":
        return "malformed"
    if parsed_usage.status != "parsed" or parsed_usage.tokens is None:
        return "none"

    required_buckets = provider_usage_buckets(config)
    known_tokens = parsed_usage.tokens.as_dict()
    known_required_buckets = [
        bucket for bucket in required_buckets if known_tokens[bucket] is not None
    ]
    if not known_required_buckets:
        return "none"
    if len(known_required_buckets) == len(required_buckets):
        return "full"
    return "partial"


def roll_up_cost_confidence(
    invocation_usages: tuple[InvocationUsage, ...],
) -> AggregateCostConfidence:
    if not invocation_usages:
        return "none"
    confidences = {usage.invocation_cost_confidence for usage in invocation_usages}
    if confidences == {"full"}:
        return "full"
    if confidences == {"none"}:
        return "none"
    if confidences <= {"full", "partial"} and "partial" in confidences:
        return "partial"
    return "mixed"
