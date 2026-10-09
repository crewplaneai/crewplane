from __future__ import annotations

import os
import platform
from dataclasses import dataclass


def is_native_windows() -> bool:
    return platform.system() == "Windows"


def supports_posix_process_groups() -> bool:
    return os.name == "posix"


@dataclass(frozen=True)
class SupportPolicy:
    """Staged feature availability, independent of native implementation selection."""

    managed_workspaces: bool
    interrupted_resume: bool
    stale_lock_recovery: bool
    tmux: bool
    native_self_update: bool


_POSIX_SUPPORT = SupportPolicy(True, True, True, True, True)
_WINDOWS_SUPPORT = SupportPolicy(False, False, False, False, False)


def support_policy() -> SupportPolicy:
    return _WINDOWS_SUPPORT if is_native_windows() else _POSIX_SUPPORT
