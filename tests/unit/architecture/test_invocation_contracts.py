from __future__ import annotations

import pytest

from orchestrator_cli.architecture.contracts import (
    LogPresentationDescriptor,
    normalize_log_presentation_profile,
    validate_log_presentation_descriptor,
)


def test_log_presentation_profile_normalizes_safe_unknown_profiles() -> None:
    assert (
        normalize_log_presentation_profile(" Vendor.Profile-1 ") == "vendor.profile-1"
    )


@pytest.mark.parametrize(
    "profile",
    ["", " ", "bad/profile", "bad profile", "x" * 65],
)
def test_log_presentation_profile_rejects_unsafe_values(profile: str) -> None:
    with pytest.raises((TypeError, ValueError)):
        normalize_log_presentation_profile(profile)


def test_log_presentation_descriptor_accepts_mapping() -> None:
    descriptor = validate_log_presentation_descriptor(
        {"format": "json_lines", "profile": "Vendor.Custom"}
    )

    assert descriptor == LogPresentationDescriptor(
        format="json_lines",
        profile="vendor.custom",
    )


def test_log_presentation_descriptor_rejects_unknown_format() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        validate_log_presentation_descriptor({"format": "yaml", "profile": "generic"})
