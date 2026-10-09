from __future__ import annotations

import hashlib
import math
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic

from crewplane.core.file_hashing import FILE_HASH_CHUNK_BYTES

DEFAULT_SNAPSHOT_MAX_ENTRIES = 250_000
DEFAULT_SNAPSHOT_MAX_FILE_BYTES = 4 * 1024 * 1024 * 1024
DEFAULT_SNAPSHOT_MAX_ELAPSED_SECONDS = 30.0


class WorkspaceSnapshotError(RuntimeError):
    """Base error for bounded workspace snapshot failures."""


class WorkspaceSnapshotLimitError(WorkspaceSnapshotError):
    """Raised when a workspace snapshot exceeds a configured resource limit."""


class WorkspaceSnapshotEntryError(WorkspaceSnapshotError):
    """Raised when a workspace contains an unsupported entry type."""


class WorkspaceSnapshotRaceError(WorkspaceSnapshotError):
    """Raised when an entry disappears or changes type during snapshotting."""


class WorkspaceSnapshotCancelled(WorkspaceSnapshotError):
    """Raised when snapshot cancellation is requested."""


@dataclass(frozen=True)
class WorkspaceSnapshotPolicy:
    max_entries: int = DEFAULT_SNAPSHOT_MAX_ENTRIES
    max_file_bytes: int = DEFAULT_SNAPSHOT_MAX_FILE_BYTES
    max_elapsed_seconds: float = DEFAULT_SNAPSHOT_MAX_ELAPSED_SECONDS
    cancel_requested: Callable[[], bool] | None = None
    clock: Callable[[], float] = monotonic
    excluded_roots: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.max_entries < 1:
            raise ValueError("Workspace snapshot max_entries must be positive.")
        if self.max_file_bytes < 0:
            raise ValueError("Workspace snapshot max_file_bytes must be nonnegative.")
        if not math.isfinite(self.max_elapsed_seconds) or self.max_elapsed_seconds <= 0:
            raise ValueError("Workspace snapshot max_elapsed_seconds must be positive.")


@dataclass
class WorkspaceSnapshotBudget:
    policy: WorkspaceSnapshotPolicy
    started_at: float
    entry_count: int = 0
    file_bytes: int = 0


def snapshot_entry_digest(
    relative: str,
    entry_stat: os.stat_result,
    kind: str,
    payload: bytes,
) -> str:
    digest = hashlib.sha256()
    mode = stat.S_IMODE(entry_stat.st_mode)
    digest.update(f"{kind}\0{relative}\0{mode:o}\0".encode())
    digest.update(payload)
    return digest.hexdigest()


def same_snapshot_entry(
    first: os.stat_result,
    second: os.stat_result,
) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def unsupported_snapshot_entry(
    relative: str,
    mode: int,
) -> WorkspaceSnapshotEntryError:
    return WorkspaceSnapshotEntryError(
        "Workspace snapshots support only directories, regular files, and "
        f"symlinks; rejected '{relative}' with mode {stat.S_IFMT(mode):#o}."
    )


def count_snapshot_entry(
    budget: WorkspaceSnapshotBudget,
    relative: str,
) -> None:
    check_snapshot_budget(budget)
    budget.entry_count += 1
    if budget.entry_count > budget.policy.max_entries:
        raise WorkspaceSnapshotLimitError(
            "Workspace snapshot entry limit exceeded "
            f"at '{relative}' ({budget.policy.max_entries})."
        )


def _reserve_snapshot_file_bytes(
    budget: WorkspaceSnapshotBudget,
    size_bytes: int,
    relative: str,
) -> None:
    budget.file_bytes += size_bytes
    if budget.file_bytes > budget.policy.max_file_bytes:
        raise WorkspaceSnapshotLimitError(
            "Workspace snapshot byte limit exceeded "
            f"at '{relative}' ({budget.policy.max_file_bytes})."
        )


def check_snapshot_budget(budget: WorkspaceSnapshotBudget) -> None:
    if budget.policy.cancel_requested is not None and budget.policy.cancel_requested():
        raise WorkspaceSnapshotCancelled("Workspace snapshot was cancelled.")
    elapsed = budget.policy.clock() - budget.started_at
    if elapsed > budget.policy.max_elapsed_seconds:
        raise WorkspaceSnapshotLimitError(
            "Workspace snapshot elapsed-time limit exceeded "
            f"({budget.policy.max_elapsed_seconds:g}s)."
        )


def hash_open_snapshot_file(
    budget: WorkspaceSnapshotBudget,
    descriptor: int,
    relative: str,
    opened_stat: os.stat_result,
) -> str:
    _reserve_snapshot_file_bytes(budget, opened_stat.st_size, relative)
    digest = hashlib.sha256()
    mode = stat.S_IMODE(opened_stat.st_mode)
    digest.update(f"file\0{relative}\0{mode:o}\0".encode())
    bytes_read = 0
    with os.fdopen(descriptor, "rb", closefd=False) as handle:
        for chunk in iter(lambda: handle.read(FILE_HASH_CHUNK_BYTES), b""):
            check_snapshot_budget(budget)
            bytes_read += len(chunk)
            if bytes_read > opened_stat.st_size:
                raise WorkspaceSnapshotRaceError(
                    f"Workspace snapshot file grew while hashing: {relative}"
                )
            digest.update(chunk)
    if bytes_read != opened_stat.st_size:
        raise WorkspaceSnapshotRaceError(
            f"Workspace snapshot file changed size while hashing: {relative}"
        )
    return digest.hexdigest()
