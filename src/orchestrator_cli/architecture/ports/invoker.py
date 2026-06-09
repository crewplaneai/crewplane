from __future__ import annotations

from typing import Protocol

from orchestrator_cli.architecture.contracts import (
    AgentInvoker,
    CanonicalIntegrationConfig,
    JsonObject,
)
from orchestrator_cli.core.config import Config


class InvokerAdapterPort(Protocol):
    """Factory contract for provider invocation integrations."""

    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: JsonObject | None = None,
    ) -> CanonicalIntegrationConfig:
        """Validate and canonicalize invoker options without side effects."""

    def create_invoker(
        self,
        config: Config,
        options: JsonObject | None = None,
    ) -> AgentInvoker:
        """Build an invoker for the configured provider transport."""
