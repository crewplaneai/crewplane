from __future__ import annotations

from typing import Any

from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import ProviderSpec

CaseData = dict[str, Any]


def provider(name: str, role: ProviderRole = ProviderRole.EXECUTOR) -> ProviderSpec:
    return ProviderSpec(provider=name, role=role)
