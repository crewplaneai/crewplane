"""Opt-in onboarding discovery; never called by planning or execution."""

import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DISCOVERY_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class CliDiscoveryRequest:
    project_root: Path
    environment: Mapping[str, str]
    executable_lookup: Callable[[str], str | None]
    timeout_seconds: float = DEFAULT_DISCOVERY_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("Discovery timeout must be a finite number greater than 0")


@dataclass(frozen=True)
class CliDiscoveredEndpoint:
    label: str
    environment: Mapping[str, str]


@dataclass(frozen=True)
class CliProviderDiscovery:
    command: tuple[str, ...]
    endpoints: tuple[CliDiscoveredEndpoint, ...]
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class CliDiscoveredModels:
    models: tuple[str, ...]
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class CliProviderOnboarding:
    """Read-only callbacks with bounded I/O and cleanup on cancellation."""

    discover: Callable[[CliDiscoveryRequest], Awaitable[CliProviderDiscovery]]
    list_models: Callable[
        [CliDiscoveryRequest, CliDiscoveredEndpoint], Awaitable[CliDiscoveredModels]
    ]
