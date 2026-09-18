from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from crewplane.adapters.invokers.cli import cli_command_available

from .rendering_config import render_provider_ready_config
from .rendering_providers import KNOWN_PROVIDER_NAMES
from .rendering_yaml_loading import load_config_mapping


@dataclass(frozen=True)
class ProviderDetection:
    provider: str
    found: bool


def detect_providers(
    default_config: str,
    project_root: Path,
    which_fn: Callable[[str], str | None],
) -> tuple[ProviderDetection, ...]:
    """Detect the commands from the profiles onboarding will actually write."""
    config = load_config_mapping(
        render_provider_ready_config(default_config, KNOWN_PROVIDER_NAMES),
        "provider detection config",
    )
    return tuple(
        ProviderDetection(
            provider,
            cli_command_available(
                config.agents[provider].cli_cmd, project_root, which_fn
            ),
        )
        for provider in KNOWN_PROVIDER_NAMES
    )
