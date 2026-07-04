from __future__ import annotations

from crewplane.core.provider_names import known_provider_names

from .rendering_errors import OnboardingRenderingError

KNOWN_PROVIDER_NAMES = known_provider_names()


def validate_known_provider(provider: str) -> None:
    if provider not in KNOWN_PROVIDER_NAMES:
        names = ", ".join(KNOWN_PROVIDER_NAMES)
        raise OnboardingRenderingError(
            f"Unknown provider '{provider}'. Expected {names}."
        )


__all__ = [
    "KNOWN_PROVIDER_NAMES",
    "validate_known_provider",
]
