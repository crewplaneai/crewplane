from __future__ import annotations


class OnboardingRenderingError(ValueError):
    """Raised when current packaged defaults cannot be transformed safely."""


__all__ = ["OnboardingRenderingError"]
