from __future__ import annotations

from typing import Literal

import crewplane.architecture.contracts as _contracts

InvocationUsage = _contracts.InvocationUsage
ProviderTokenUsage = _contracts.ProviderTokenUsage

VisibleEstimateMethod = Literal["char-count-lower-bound"]
VISIBLE_ESTIMATE_METHOD: VisibleEstimateMethod = "char-count-lower-bound"
