"""Built-in implementation aliases for runtime adapters."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

_INTEGRATION_ALIASES: dict[str, dict[str, str]] = {
    "invoker": {
        "cli": "crewplane.adapters.invokers.cli:CliInvokerAdapter",
        "mock": "crewplane.adapters.invokers.mock:MockInvokerAdapter",
    },
    "ui": {
        "tmux": "crewplane.adapters.ui.tmux:TmuxUIAdapter",
        "none": "crewplane.adapters.ui.null:NullUIAdapter",
    },
    "artifacts": {
        "filesystem": (
            "crewplane.adapters.artifacts.filesystem:FilesystemArtifactsAdapter"
        ),
    },
}
INTEGRATION_ALIAS_REGISTRY: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        integration_kind: MappingProxyType(aliases)
        for integration_kind, aliases in _INTEGRATION_ALIASES.items()
    }
)


def allowed_implementations(integration_kind: str) -> list[str]:
    implementations = INTEGRATION_ALIAS_REGISTRY.get(integration_kind)
    if implementations is None:
        return []
    return sorted(implementations)
