from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from crewplane.adapters.invokers.cli import (
    inspect_cli_command,
)

from .rendering_config import render_provider_ready_config
from .rendering_providers import KNOWN_PROVIDER_NAMES
from .rendering_yaml_loading import load_config_mapping


@dataclass(frozen=True)
class ProviderDetection:
    provider: str
    found: bool
    diagnostic: str | None = None


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
    detections = []
    for provider in KNOWN_PROVIDER_NAMES:
        result = inspect_cli_command(
            config.agents[provider].cli_cmd, project_root, which_fn
        )
        detections.append(
            ProviderDetection(provider, result.available, result.diagnostic)
        )
    return tuple(detections)
