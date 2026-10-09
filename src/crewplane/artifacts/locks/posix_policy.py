from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep

from .manifest import LockManifestError, LockRunMetadata, finalize_stale_running_run
from .ownership import (
    LOCK_OWNER_FILENAME,
    LockActivity,
    LockOwner,
    ResumeLockError,
    read_owner,
)
from .process_identity import ProcessIdentity, ProcessInspector
from .provider_processes import ensure_no_live_provider_processes


@dataclass(frozen=True)
class _LockDirectoryIdentity:
    device: int
    inode: int
    ctime_ns: int


@dataclass
class _OwnerlessLockGrace:
    grace_seconds: float
    lock_identity: _LockDirectoryIdentity | None = None
    observed_at: float = 0.0

    def should_wait(self, lock_dir: Path) -> bool:
        identity = _lock_directory_identity(lock_dir)
        if identity is None:
            return True
        now = monotonic()
        if identity != self.lock_identity:
            self.lock_identity = identity
            self.observed_at = now
        return now - self.observed_at < self.grace_seconds


class PosixLockPolicy:
    def __init__(self, grace_seconds: float = 1.0) -> None:
        self.ownerless_grace = _OwnerlessLockGrace(grace_seconds)

    def recover_collision(
        self,
        lock_dir: Path,
        state_dir: Path,
        workflow_identity: str,
        workflow_signature: str,
        inspector: ProcessInspector,
    ) -> None:
        _recover_or_raise(
            lock_dir,
            state_dir,
            workflow_identity,
            workflow_signature,
            self.ownerless_grace,
            inspector,
        )

    def can_release(self, terminal_complete: bool, cleanup_unconfirmed: bool) -> bool:
        del cleanup_unconfirmed
        return terminal_complete

    def activity(
        self, locks_root: Path, run_key_name: str, inspector: ProcessInspector
    ) -> LockActivity:
        matched_stale = False
        for lock_dir in sorted(locks_root.iterdir()):
            if not lock_dir.is_dir() or lock_dir.is_symlink():
                continue
            owner = read_owner(lock_dir)
            if owner is None:
                return "unverifiable"
            if owner.run_key_name != run_key_name:
                continue
            if _owner_process_is_live(owner, inspector):
                return "live"
            matched_stale = True
        return "stale" if matched_stale else "none"


def _recover_or_raise(
    lock_dir: Path,
    state_dir: Path,
    workflow_identity: str,
    workflow_signature: str,
    ownerless_grace: _OwnerlessLockGrace,
    inspector: ProcessInspector,
) -> None:
    owner = read_owner(lock_dir)
    if owner is None:
        if ownerless_grace.should_wait(lock_dir):
            sleep(0.05)
            return
        _recover_unowned_lock(lock_dir)
        return
    if owner.workflow_identity != workflow_identity:
        raise ResumeLockError("Same-context lock owner identity does not match.")
    if owner.workflow_signature != workflow_signature:
        raise ResumeLockError("Same-context lock owner signature does not match.")
    if _owner_process_is_live(owner, inspector):
        raise ResumeLockError(
            "A live same-context workflow run already holds the lock."
        )
    metadata = LockRunMetadata(
        run_id=owner.run_id,
        run_key_name=owner.run_key_name,
        workflow_identity=owner.workflow_identity,
        workflow_signature=owner.workflow_signature,
        terminal_recovery=owner.terminal_recovery,
    )
    try:
        ensure_no_live_provider_processes(state_dir, metadata, inspector)
    except LockManifestError as exc:
        raise ResumeLockError(str(exc)) from exc
    _recover_stale_lock(
        lock_dir,
        state_dir,
        owner,
        workflow_identity,
        workflow_signature,
        inspector,
    )


def _recover_unowned_lock(lock_dir: Path) -> None:
    recovery_dir = _recovery_dir(lock_dir)
    lock_dir.replace(recovery_dir)
    try:
        if any(recovery_dir.iterdir()):
            raise ResumeLockError("Recovered lock directory contains unexpected files.")
        shutil.rmtree(recovery_dir, ignore_errors=True)
    except Exception:
        _restore_recovery_dir(lock_dir, recovery_dir)
        raise


def _lock_directory_identity(lock_dir: Path) -> _LockDirectoryIdentity | None:
    try:
        stat_result = lock_dir.lstat()
    except FileNotFoundError:
        return None
    except PermissionError:
        raise
    except OSError as exc:
        raise ResumeLockError("Cannot inspect same-context lock directory.") from exc
    return _LockDirectoryIdentity(
        device=stat_result.st_dev,
        inode=stat_result.st_ino,
        ctime_ns=stat_result.st_ctime_ns,
    )


def _recover_stale_lock(
    lock_dir: Path,
    state_dir: Path,
    owner: LockOwner,
    workflow_identity: str,
    workflow_signature: str,
    inspector: ProcessInspector,
) -> None:
    recovery_dir = _recovery_dir(lock_dir)
    lock_dir.replace(recovery_dir)
    try:
        reread_owner = _read_recovered_owner(
            recovery_dir,
            owner,
            workflow_identity,
            workflow_signature,
            inspector,
        )
        _ensure_only_owner_file(recovery_dir)
        metadata = LockRunMetadata(
            run_id=reread_owner.run_id,
            run_key_name=reread_owner.run_key_name,
            workflow_identity=reread_owner.workflow_identity,
            workflow_signature=reread_owner.workflow_signature,
            terminal_recovery=reread_owner.terminal_recovery,
        )
        try:
            finalize_stale_running_run(state_dir, metadata)
        except LockManifestError as exc:
            raise ResumeLockError(str(exc)) from exc
        shutil.rmtree(recovery_dir, ignore_errors=True)
    except Exception:
        _restore_recovery_dir(lock_dir, recovery_dir)
        raise


def _read_recovered_owner(
    recovery_dir: Path,
    original_owner: LockOwner,
    workflow_identity: str,
    workflow_signature: str,
    inspector: ProcessInspector,
) -> LockOwner:
    reread_owner = read_owner(recovery_dir)
    if reread_owner is None or reread_owner.owner_token != original_owner.owner_token:
        raise ResumeLockError("Recovered lock ownership changed during takeover.")
    if reread_owner.workflow_identity != workflow_identity:
        raise ResumeLockError("Recovered lock owner identity does not match.")
    if reread_owner.workflow_signature != workflow_signature:
        raise ResumeLockError("Recovered lock owner signature does not match.")
    if _owner_process_is_live(reread_owner, inspector):
        raise ResumeLockError(
            "A live same-context workflow run already holds the lock."
        )
    return reread_owner


def _owner_process_is_live(owner: LockOwner, inspector: ProcessInspector) -> bool:
    identity = ProcessIdentity(
        pid=owner.pid,
        hostname=owner.hostname,
        start_identity=owner.process_start_identity,
    )
    try:
        return inspector.is_live(identity)
    except RuntimeError as exc:
        raise ResumeLockError(str(exc)) from exc


def _restore_recovery_dir(lock_dir: Path, recovery_dir: Path) -> None:
    if not recovery_dir.exists() or lock_dir.exists():
        return
    try:
        recovery_dir.replace(lock_dir)
    except OSError:
        return


def _ensure_only_owner_file(lock_dir: Path) -> None:
    names = {path.name for path in lock_dir.iterdir()}
    if names - {LOCK_OWNER_FILENAME}:
        raise ResumeLockError("Recovered lock directory contains unexpected files.")


def _recovery_dir(lock_dir: Path) -> Path:
    return lock_dir.with_name(f"{lock_dir.name}.recover-{uuid.uuid4().hex}")
