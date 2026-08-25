from __future__ import annotations

from typing import Protocol

from crewplane.architecture.contracts import AgentInvoker, JsonObject
from crewplane.core.config import Config

from .options import IntegrationOptionsCanonicalizerPort


class InvokerAdapterPort(IntegrationOptionsCanonicalizerPort, Protocol):
    """Factory contract for provider invocation integrations."""

    def create_invoker(
        self,
        config: Config,
        options: JsonObject | None = None,
    ) -> AgentInvoker:
        """Build an invoker for the configured provider transport."""
