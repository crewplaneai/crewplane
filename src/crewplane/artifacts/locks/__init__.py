from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import monotonic, sleep

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from ..atomic import atomic_write_json, atomic_write_json_if_absent
from ..naming import build_lock_name, validate_run_key_name
from .manifest import (
    LockManifestError,
    LockRunMetadata,
    finalize_stale_running_run,
)
from .process_identity import ProcessIdentity, ProcessInspector

LOCK_OWNER_FILENAME = "owner.json"


class ResumeLockError(RuntimeError):
    """Raised when a same-context resume lock cannot be acquired safely."""


class LockOwner(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_token: str
    pid: int
    hostname: str
    process_start_identity: str | None = None
    acquired_at: str
    workflow_identity: str
    workflow_signature: str
    run_id: str | None = None
    run_key_name: str | None = None

    @field_validator("run_key_name")
    @classmethod
    def _validate_run_key_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_run_key_name(value)


@dataclass
class SameContextLock:
    lock_dir: Path
    owner_token: str

    def update_run(self, run_id: str, run_key_name: str) -> None:
        validated_run_key_name = validate_run_key_name(run_key_name)
        owner = _read_owner(self.lock_dir)
        if owner is None or owner.owner_token != self.owner_token:
            raise ResumeLockError("Cannot update a lock owned by another process.")
        updated = owner.model_copy(
            update={"run_id": run_id, "run_key_name": validated_run_key_name}
        )
        _write_owner(self.lock_dir, updated)

    def release(self) -> None:
        owner = _read_owner(self.lock_dir)
        if owner is None or owner.owner_token != self.owner_token:
            return
        try:
            (self.lock_dir / LOCK_OWNER_FILENAME).unlink()
            self.lock_dir.rmdir()
        except OSError:
            return


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
    owner = _new_owner(owner_token, workflow_identity, workflow_signature, inspector)
    ownerless_grace = _OwnerlessLockGrace(grace_seconds=grace_seconds)
    while True:
        try:
            lock_dir.mkdir(exist_ok=False)
            _write_new_owner(lock_dir, owner)
            return SameContextLock(lock_dir=lock_dir, owner_token=owner_token)
        except FileExistsError:
            _recover_or_raise(
                lock_dir,
                state_dir,
                workflow_identity,
                workflow_signature,
                ownerless_grace,
                inspector,
            )


def _recover_or_raise(
    lock_dir: Path,
    state_dir: Path,
    workflow_identity: str,
    workflow_signature: str,
    ownerless_grace: _OwnerlessLockGrace,
    inspector: ProcessInspector,
) -> None:
    owner = _read_owner(lock_dir)
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
        try:
            finalize_stale_running_run(
                state_dir,
                LockRunMetadata(
                    run_id=reread_owner.run_id,
                    run_key_name=reread_owner.run_key_name,
                    workflow_identity=reread_owner.workflow_identity,
                    workflow_signature=reread_owner.workflow_signature,
                ),
            )
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
    reread_owner = _read_owner(recovery_dir)
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


def _new_owner(
    owner_token: str,
    workflow_identity: str,
    workflow_signature: str,
    inspector: ProcessInspector,
) -> LockOwner:
    identity = inspector.current()
    return LockOwner(
        owner_token=owner_token,
        pid=identity.pid,
        hostname=identity.hostname,
        process_start_identity=identity.start_identity,
        acquired_at=datetime.now().isoformat(),
        workflow_identity=workflow_identity,
        workflow_signature=workflow_signature,
    )


def _read_owner(lock_dir: Path) -> LockOwner | None:
    try:
        payload = json.loads(
            (lock_dir / LOCK_OWNER_FILENAME).read_text(encoding="utf-8")
        )
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    try:
        return LockOwner.model_validate(payload)
    except ValidationError:
        return None


def _write_owner(lock_dir: Path, owner: LockOwner) -> None:
    atomic_write_json(
        lock_dir / LOCK_OWNER_FILENAME,
        owner.model_dump(mode="json", exclude_none=True),
        ensure_parent=False,
    )


def _write_new_owner(lock_dir: Path, owner: LockOwner) -> None:
    atomic_write_json_if_absent(
        lock_dir / LOCK_OWNER_FILENAME,
        owner.model_dump(mode="json", exclude_none=True),
        ensure_parent=False,
    )


def _ensure_only_owner_file(lock_dir: Path) -> None:
    names = {path.name for path in lock_dir.iterdir()}
    if names - {LOCK_OWNER_FILENAME}:
        raise ResumeLockError("Recovered lock directory contains unexpected files.")


def _recovery_dir(lock_dir: Path) -> Path:
    return lock_dir.with_name(f"{lock_dir.name}.recover-{uuid.uuid4().hex}")
