from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from crewplane.architecture.safe_file_reads import read_contained_bytes

from ..atomic import atomic_write_json, atomic_write_json_if_absent
from ..naming import validate_run_key_name
from .manifest import TerminalRecoveryIntent
from .process_identity import ProcessInspector

LOCK_OWNER_FILENAME = "owner.json"
type LockActivity = Literal["live", "stale", "none", "unverifiable"]


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
    terminal_recovery: TerminalRecoveryIntent | None = None

    @field_validator("run_key_name")
    @classmethod
    def _validate_run_key_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_run_key_name(value)


def new_owner(
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


def read_owner(lock_dir: Path) -> LockOwner | None:
    try:
        payload = json.loads(read_contained_bytes(lock_dir, LOCK_OWNER_FILENAME))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    try:
        return LockOwner.model_validate(payload)
    except ValidationError:
        return None


def write_owner(lock_dir: Path, owner: LockOwner) -> None:
    atomic_write_json(
        lock_dir / LOCK_OWNER_FILENAME,
        owner.model_dump(mode="json", exclude_none=True),
        ensure_parent=False,
    )


def write_new_owner(lock_dir: Path, owner: LockOwner) -> None:
    atomic_write_json_if_absent(
        lock_dir / LOCK_OWNER_FILENAME,
        owner.model_dump(mode="json", exclude_none=True),
        ensure_parent=False,
    )
