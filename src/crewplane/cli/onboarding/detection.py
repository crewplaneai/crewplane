from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from importlib import metadata
from pathlib import Path

from crewplane.adapters.invokers.cli import (
    inspect_cli_command,
)
from crewplane.adapters.invokers.cli_invoker.provider_registrations import (
    missing_provider_plugin_message,
)

from .rendering_config import render_provider_ready_config
from .rendering_providers import KNOWN_PROVIDER_NAMES
from .rendering_yaml_loading import load_config_mapping


class ProviderStatus(StrEnum):
    AVAILABLE = "available to configure"
    COMMAND_MISSING = "command not found"
    PLUGIN_MISSING = "plugin not installed"


@dataclass(frozen=True)
class ProviderDetection:
    provider: str
    status: ProviderStatus
    diagnostic: str | None = None

    @property
    def available(self) -> bool:
        return self.status == ProviderStatus.AVAILABLE


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
            ProviderDetection(
                provider,
                ProviderStatus.AVAILABLE
                if result.available
                else ProviderStatus.COMMAND_MISSING,
                result.diagnostic,
            )
        )
    try:
        version = metadata.version("crewplane-ollama")
    except metadata.PackageNotFoundError:
        detections.append(
            ProviderDetection(
                "local",
                ProviderStatus.PLUGIN_MISSING,
                missing_provider_plugin_message("crewplane-ollama"),
            )
        )
    else:
        detections.append(
            ProviderDetection(
                "local",
                ProviderStatus.AVAILABLE,
                f"Ollama plugin {version} is installed. Select Ollama to choose a server and model.",
            )
        )
    return tuple(detections)
