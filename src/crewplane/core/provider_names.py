from __future__ import annotations

from crewplane.architecture.contracts import SUPPORTED_PROVIDER_KINDS, ProviderKind


def known_provider_names() -> tuple[str, ...]:
    """Return built-in provider names that map to real provider CLIs."""

    return tuple(
        provider_kind.value
        for provider_kind in SUPPORTED_PROVIDER_KINDS
        if provider_kind != ProviderKind.GENERIC
    )


def normalize_provider_name(value: object, field_name: str) -> object:
    """Normalize provider identifiers at config and workflow boundaries."""

    if not isinstance(value, str):
        return value
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must be a non-empty string")
    return normalized
