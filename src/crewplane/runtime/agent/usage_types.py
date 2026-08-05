from __future__ import annotations

from typing import Literal

import crewplane.architecture.contracts as _contracts
from crewplane.architecture.contracts import (
    ProviderKind,
)

InvocationUsage = _contracts.InvocationUsage
ProviderTokenUsage = _contracts.ProviderTokenUsage

VisibleEstimateMethod = Literal["char-count-lower-bound"]
TokenBucket = Literal[
    "input",
    "cached_input",
    "cache_write",
    "output",
    "reasoning",
    "total",
]
VISIBLE_ESTIMATE_METHOD: VisibleEstimateMethod = "char-count-lower-bound"
STRUCTURED_PROVIDER_KINDS: frozenset[ProviderKind] = frozenset(
    {
        ProviderKind.CLAUDE,
        ProviderKind.CODEX,
        ProviderKind.GEMINI,
        ProviderKind.KILO,
    }
)
