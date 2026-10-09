from __future__ import annotations

from pathlib import Path

from .ownership import LockActivity, ResumeLockError
from .process_identity import ProcessInspector


class WindowsLockPolicy:
    def recover_collision(
        self,
        lock_dir: Path,
        state_dir: Path,
        workflow_identity: str,
        workflow_signature: str,
        inspector: ProcessInspector,
    ) -> None:
        del state_dir, workflow_identity, workflow_signature, inspector
        raise ResumeLockError(
            f"Same-context lock already exists: {lock_dir}. "
            "Automatic lock recovery is unsupported on native Windows."
        ) from None

    def activity(
        self, locks_root: Path, run_key_name: str, inspector: ProcessInspector
    ) -> LockActivity:
        del run_key_name, inspector
        return "unverifiable" if any(locks_root.iterdir()) else "none"

    def can_release(self, terminal_complete: bool, cleanup_unconfirmed: bool) -> bool:
        return terminal_complete and not cleanup_unconfirmed
