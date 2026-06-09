from __future__ import annotations

__all__ = ["MockInvokerAdapter"]

from orchestrator_cli.architecture.contracts import (
    AgentInvoker,
    CanonicalIntegrationConfig,
    JsonObject,
)
from orchestrator_cli.core.config import Config

from .mock_invoker import MockAgentInvoker, parse_options


class MockInvokerAdapter:
    """Create deterministic mock invokers for local orchestration runs."""

    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: JsonObject | None = None,
    ) -> CanonicalIntegrationConfig:
        parsed = parse_options(options)
        canonical_options = {
            "delay_seconds": parsed.delay_seconds,
            "fail_when": [selector.__dict__ for selector in parsed.fail_when],
            "observation_delay_seconds": parsed.observation_delay_seconds,
            "output_dir": parsed.output_dir,
            "output_mode": parsed.output_mode,
            "seed": parsed.seed,
            "strict_file_mode": parsed.strict_file_mode,
        }
        return CanonicalIntegrationConfig(
            implementation=implementation,
            resolved_identity=resolved_identity,
            options=canonical_options,
            option_scopes={key: "execution" for key in canonical_options},
        )

    def create_invoker(
        self,
        config: Config,
        options: JsonObject | None = None,
    ) -> AgentInvoker:
        """Build a mock invoker from the configured integration options."""

        _validate_config(config)
        return MockAgentInvoker(options=parse_options(options))


def _validate_config(config: Config) -> None:
    if not isinstance(config, Config):
        raise TypeError("config must be a Config instance")
