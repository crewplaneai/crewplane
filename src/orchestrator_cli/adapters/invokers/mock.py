from __future__ import annotations

__all__ = ["MockInvokerAdapter"]

from collections.abc import Mapping
from typing import Any

from orchestrator_cli.architecture.api_version import EXT_API_VERSION
from orchestrator_cli.core.config import Config
from orchestrator_cli.core.preflight.runtime_config import CanonicalIntegrationConfig
from orchestrator_cli.runtime.agent.types import AgentInvoker

from .mock_invoker import MockAgentInvoker, parse_options


class MockInvokerAdapter:
    """Create deterministic mock invokers for local orchestration runs."""

    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: Mapping[str, Any] | None = None,
    ) -> CanonicalIntegrationConfig:
        parsed = parse_options(options)
        canonical_options = {
            "delay_seconds": parsed.delay_seconds,
            "fail_when": [dict(selector.criteria) for selector in parsed.fail_when],
            "observation_delay_seconds": parsed.observation_delay_seconds,
            "output_dir": (
                parsed.output_dir.as_posix() if parsed.output_dir is not None else None
            ),
            "output_mode": parsed.output_mode,
            "seed": parsed.seed,
            "strict_file_mode": parsed.strict_file_mode,
        }
        return CanonicalIntegrationConfig(
            implementation=implementation,
            resolved_identity=resolved_identity,
            api_version=EXT_API_VERSION,
            options=canonical_options,
            option_scopes={key: "execution" for key in canonical_options},
        )

    def create_invoker(
        self,
        config: Config,  # noqa: ARG002 - Required by callback or protocol signature.
        options: Mapping[str, Any] | None = None,
    ) -> AgentInvoker:
        """Build a mock invoker from the configured integration options."""

        return MockAgentInvoker(options=parse_options(options))
