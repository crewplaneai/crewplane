from __future__ import annotations


def normalize_provider_name(value: object, field_name: str) -> object:
    """Normalize provider identifiers at config and workflow boundaries."""

    if not isinstance(value, str):
        return value
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must be a non-empty string")
    return normalized
