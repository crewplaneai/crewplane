from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from crewplane.architecture.contracts import (
    InvocationUsage as InvocationUsage,
)
from crewplane.architecture.contracts import (
    ProviderKind,
    UsageParserProfile,
)
from crewplane.architecture.contracts import (
    ProviderTokenUsage as ProviderTokenUsage,
)

VisibleEstimateMethod = Literal["char-count-lower-bound"]
UsageParser = UsageParserProfile
TokenBucket = Literal[
    "input",
    "cached_input",
    "cache_write",
    "output",
    "reasoning",
    "total",
]
TOKEN_BUCKETS: tuple[TokenBucket, ...] = (
    "input",
    "cached_input",
    "cache_write",
    "output",
    "reasoning",
    "total",
)
VISIBLE_ESTIMATE_METHOD: VisibleEstimateMethod = "char-count-lower-bound"
STRUCTURED_PROVIDER_KINDS: frozenset[ProviderKind] = frozenset(
    {ProviderKind.CLAUDE, ProviderKind.CODEX}
)
TOKEN_KEY_ALIASES: dict[TokenBucket, tuple[str, ...]] = {
    "input": ("input_tokens", "prompt_tokens", "input"),
    "cached_input": ("cached_input_tokens", "cache_read_input_tokens"),
    "cache_write": ("cache_write_tokens",),
    "output": ("output_tokens", "completion_tokens", "output"),
    "reasoning": ("reasoning_tokens",),
    "total": ("total_tokens", "total"),
}


@dataclass(frozen=True)
class ParsedProviderUsage:
    status: Literal["parsed", "none", "malformed"]
    tokens: ProviderTokenUsage | None = None
    error: str | None = None
