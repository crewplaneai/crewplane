from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from crewplane.core.execution_state import TerminalRunStatus

from ..naming import build_lock_name, validate_run_key_name
from . import policy
from .manifest import (
    TERMINAL_RECOVERY_PHASES,
    TerminalRecoveryIntent,
    TerminalRecoveryPhase,
)
from .ownership import (
    LOCK_OWNER_FILENAME,
    LockActivity,
    ResumeLockError,
    new_owner,
    read_owner,
    write_new_owner,
    write_owner,
)
from .process_identity import ProcessInspector

__all__ = [
    "LOCK_OWNER_FILENAME",
    "LockActivity",
    "ResumeLockError",
    "SameContextLock",
    "acquire_same_context_lock",
    "run_lock_activity",
]


def run_lock_activity(
    state_dir: Path,
    run_key_name: str,
    process_inspector: ProcessInspector | None = None,
) -> LockActivity:
    """Return ``live``, ``stale``, ``none``, or ``unverifiable`` for a run lock."""

    inspector = process_inspector or ProcessInspector()
    locks_root = state_dir / "locks"
    if not locks_root.exists():
        return "none"
    if not locks_root.is_dir() or locks_root.is_symlink():
        return "unverifiable"
    return policy.lock_policy().activity(locks_root, run_key_name, inspector)


@dataclass
class SameContextLock:
    lock_dir: Path
    owner_token: str
    policy: policy.LockPolicy

    def update_run(self, run_id: str, run_key_name: str) -> None:
        validated_run_key_name = validate_run_key_name(run_key_name)
        owner = read_owner(self.lock_dir)
        if owner is None or owner.owner_token != self.owner_token:
            raise ResumeLockError("Cannot update a lock owned by another process.")
        updated = owner.model_copy(
            update={"run_id": run_id, "run_key_name": validated_run_key_name}
        )
        write_owner(self.lock_dir, updated)

    def record_terminal_recovery(
        self,
        phase: TerminalRecoveryPhase,
        status: TerminalRunStatus,
        reason: str | None,
    ) -> None:
        owner = read_owner(self.lock_dir)
        if owner is None or owner.owner_token != self.owner_token:
            raise ResumeLockError("Cannot update a lock owned by another process.")
        if owner.run_id is None or owner.run_key_name is None:
            raise ResumeLockError(
                "Cannot record terminal recovery before run ownership."
            )
        recovery = TerminalRecoveryIntent(
            phase=phase,
            status=status,
            reason=reason,
        )
        previous = owner.terminal_recovery
        if previous is None:
            if phase != TERMINAL_RECOVERY_PHASES[0]:
                raise ResumeLockError(
                    "Terminal recovery must begin with outcome_selected."
                )
        else:
            if previous.status != status or previous.reason != reason:
                raise ResumeLockError("Terminal recovery outcome cannot change.")
            if previous.phase == phase:
                return
            previous_index = TERMINAL_RECOVERY_PHASES.index(previous.phase)
            phase_index = TERMINAL_RECOVERY_PHASES.index(phase)
            if phase_index != previous_index + 1:
                raise ResumeLockError("Terminal recovery phase cannot skip or regress.")
        write_owner(
            self.lock_dir,
            owner.model_copy(update={"terminal_recovery": recovery}),
        )

    def release(
        self, terminal_complete: bool = True, cleanup_unconfirmed: bool = False
    ) -> None:
        if not self.policy.can_release(terminal_complete, cleanup_unconfirmed):
            return
        owner = read_owner(self.lock_dir)
        if owner is None or owner.owner_token != self.owner_token:
            return
        try:
            (self.lock_dir / LOCK_OWNER_FILENAME).unlink()
            self.lock_dir.rmdir()
        except OSError:
            return


def acquire_same_context_lock(
    state_dir: Path,
    workflow_name: str,
    workflow_identity: str,
    workflow_signature: str,
    grace_seconds: float = 1.0,
    process_inspector: ProcessInspector | None = None,
) -> SameContextLock:
    inspector = process_inspector or ProcessInspector()
    owner_token = uuid.uuid4().hex
    locks_root = state_dir / "locks"
    locks_root.mkdir(parents=True, exist_ok=True)
    lock_dir = locks_root / build_lock_name(
        workflow_name,
        workflow_identity,
        workflow_signature,
    )
    owner = new_owner(owner_token, workflow_identity, workflow_signature, inspector)
    selected_policy = policy.lock_policy(grace_seconds)
    while True:
        try:
            lock_dir.mkdir(exist_ok=False)
            write_new_owner(lock_dir, owner)
            return SameContextLock(
                lock_dir=lock_dir, owner_token=owner_token, policy=selected_policy
            )
        except FileExistsError:
            selected_policy.recover_collision(
                lock_dir,
                state_dir,
                workflow_identity,
                workflow_signature,
                inspector,
            )
