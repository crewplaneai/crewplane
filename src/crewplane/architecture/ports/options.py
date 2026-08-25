from __future__ import annotations

from typing import Protocol

from crewplane.architecture.contracts import CanonicalIntegrationConfig, JsonObject


class IntegrationOptionsCanonicalizerPort(Protocol):
    """Canonicalize adapter options without creating runtime resources."""

    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: JsonObject | None = None,
    ) -> CanonicalIntegrationConfig:
        """Validate options and return their deterministic integration contract."""
