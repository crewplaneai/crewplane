from __future__ import annotations

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
