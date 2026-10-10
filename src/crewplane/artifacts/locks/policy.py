from __future__ import annotations

from pathlib import Path
from typing import Protocol

from crewplane.core.platform import is_native_windows

from .ownership import LockActivity
from .process_identity import ProcessInspector


class LockPolicy(Protocol):
    def recover_collision(
        self,
        lock_dir: Path,
        state_dir: Path,
        workflow_identity: str,
        workflow_signature: str,
        inspector: ProcessInspector,
    ) -> None: ...

    def activity(
        self, locks_root: Path, run_key_name: str, inspector: ProcessInspector
    ) -> LockActivity: ...

    def can_release(
        self, terminal_complete: bool, cleanup_unconfirmed: bool
    ) -> bool: ...


def lock_policy(grace_seconds: float = 1.0) -> LockPolicy:
    if is_native_windows():
        from .windows_policy import WindowsLockPolicy

        return WindowsLockPolicy()
    from .posix_policy import PosixLockPolicy

    return PosixLockPolicy(grace_seconds)
