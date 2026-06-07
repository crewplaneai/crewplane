from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from orchestrator_cli.core.config import Config
from orchestrator_cli.core.preflight.runtime_config import CanonicalIntegrationConfig
from orchestrator_cli.runtime.agent.types import AgentInvoker


class InvokerAdapterPort(Protocol):
    """Factory contract for provider invocation integrations."""

    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: Mapping[str, Any] | None = None,
    ) -> CanonicalIntegrationConfig:
        """Validate and canonicalize invoker options without side effects."""

    def create_invoker(
        self,
        config: Config,
        options: Mapping[str, Any] | None = None,
    ) -> AgentInvoker:
        """Build an invoker for the configured provider transport."""
