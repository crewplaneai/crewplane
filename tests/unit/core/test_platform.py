from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from crewplane.core import platform as platform_policy


def test_is_native_windows_uses_platform_system(monkeypatch) -> None:
    monkeypatch.setattr(platform_policy.platform, "system", lambda: "Windows")
    assert platform_policy.is_native_windows() is True

    monkeypatch.setattr(platform_policy.platform, "system", lambda: "Linux")
    assert platform_policy.is_native_windows() is False


def test_supports_posix_process_groups_uses_os_name(monkeypatch) -> None:
    monkeypatch.setattr(platform_policy.os, "name", "posix")
    assert platform_policy.supports_posix_process_groups() is True

    monkeypatch.setattr(platform_policy.os, "name", "nt")
    assert platform_policy.supports_posix_process_groups() is False


@pytest.mark.parametrize(
    "system, available", [("Windows", False), ("Linux", True), ("Darwin", True)]
)
def test_support_policy_preserves_staged_availability(monkeypatch, system, available):
    monkeypatch.setattr(platform_policy.platform, "system", lambda: system)
    policy = platform_policy.support_policy()
    assert policy == platform_policy.SupportPolicy(*([available] * 5))
    with pytest.raises(FrozenInstanceError):
        policy.tmux = not available
