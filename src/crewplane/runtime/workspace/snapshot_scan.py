from __future__ import annotations

import hashlib
import math
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
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
class _WorkspaceSnapshotBudget:
    policy: WorkspaceSnapshotPolicy
    started_at: float
    entry_count: int = 0
    file_bytes: int = 0


def snapshot_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for relative_path, entry_digest in snapshot_entries(root).items():
        digest.update(f"{relative_path}\0{entry_digest}\0".encode())
    return digest.hexdigest()


def snapshot_entries(
    root: Path,
    policy: WorkspaceSnapshotPolicy | None = None,
) -> dict[str, str]:
    resolved_policy = policy or WorkspaceSnapshotPolicy()
    budget = _WorkspaceSnapshotBudget(
        policy=resolved_policy,
        started_at=resolved_policy.clock(),
    )
    root_stat = _require_snapshot_directory(root)
    entries: dict[str, str] = {}
    root_descriptor = _open_snapshot_directory(root, root_stat, ".")
    try:
        _scan_snapshot_directory(budget, root_descriptor, "", entries)
    finally:
        os.close(root_descriptor)
    return dict(sorted(entries.items()))


def _snapshot_entry_digest(
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


def _scan_snapshot_directory(
    budget: _WorkspaceSnapshotBudget,
    directory_descriptor: int,
    relative_parent: str,
    entries: dict[str, str],
) -> None:
    discovered = _discover_snapshot_entries(
        budget, directory_descriptor, relative_parent
    )
    for name, relative, entry_stat in sorted(discovered):
        mode = entry_stat.st_mode
        if stat.S_ISLNK(mode):
            entries[relative] = _snapshot_entry_digest(
                relative,
                entry_stat,
                "symlink",
                _read_snapshot_symlink_at(
                    directory_descriptor,
                    name,
                    relative,
                    entry_stat,
                ),
            )
            continue
        if stat.S_ISDIR(mode):
            child_descriptor = _open_snapshot_directory_at(
                directory_descriptor,
                name,
                entry_stat,
                relative,
            )
            try:
                opened_stat = os.fstat(child_descriptor)
                entries[relative] = _snapshot_entry_digest(
                    relative,
                    opened_stat,
                    "dir",
                    b"",
                )
                _scan_snapshot_directory(
                    budget,
                    child_descriptor,
                    relative,
                    entries,
                )
            finally:
                os.close(child_descriptor)
            continue
        if not stat.S_ISREG(mode):
            raise _unsupported_snapshot_entry(relative, mode)
        entries[relative] = _snapshot_regular_file_digest(
            budget,
            directory_descriptor,
            name,
            relative,
            entry_stat,
        )


def _discover_snapshot_entries(
    budget: _WorkspaceSnapshotBudget,
    directory_descriptor: int,
    relative_parent: str,
) -> list[tuple[str, str, os.stat_result]]:
    _check_snapshot_budget(budget)
    discovered: list[tuple[str, str, os.stat_result]] = []
    try:
        with os.scandir(directory_descriptor) as iterator:
            for entry in iterator:
                relative = (
                    f"{relative_parent}/{entry.name}" if relative_parent else entry.name
                )
                if relative in budget.policy.excluded_roots:
                    continue
                _count_snapshot_entry(budget, relative)
                discovered.append(
                    (
                        entry.name,
                        relative,
                        _snapshot_lstat_at(
                            directory_descriptor,
                            entry.name,
                            relative,
                        ),
                    )
                )
    except WorkspaceSnapshotError:
        raise
    except OSError as exc:
        location = relative_parent or "."
        raise WorkspaceSnapshotRaceError(
            f"Workspace snapshot directory changed while scanning: {location}"
        ) from exc
    return discovered


def _snapshot_regular_file_digest(
    budget: _WorkspaceSnapshotBudget,
    directory_descriptor: int,
    name: str,
    relative: str,
    entry_stat: os.stat_result,
) -> str:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    except OSError as exc:
        raise WorkspaceSnapshotRaceError(
            f"Workspace snapshot entry changed before open: {relative}"
        ) from exc
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode) or (
            opened_stat.st_dev,
            opened_stat.st_ino,
        ) != (entry_stat.st_dev, entry_stat.st_ino):
            raise WorkspaceSnapshotRaceError(
                f"Workspace snapshot entry changed type or identity: {relative}"
            )
        _reserve_snapshot_file_bytes(budget, opened_stat.st_size, relative)
        digest = hashlib.sha256()
        mode = stat.S_IMODE(opened_stat.st_mode)
        digest.update(f"file\0{relative}\0{mode:o}\0".encode())
        bytes_read = 0
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            for chunk in iter(lambda: handle.read(FILE_HASH_CHUNK_BYTES), b""):
                _check_snapshot_budget(budget)
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
    finally:
        os.close(descriptor)


def _require_snapshot_directory(root: Path) -> os.stat_result:
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise WorkspaceSnapshotRaceError(
            f"Workspace snapshot root is unavailable: {root}"
        ) from exc
    if not stat.S_ISDIR(root_stat.st_mode):
        raise WorkspaceSnapshotEntryError(
            f"Workspace snapshot root is not a directory: {root}"
        )
    return root_stat


def _snapshot_lstat_at(
    directory_descriptor: int,
    name: str,
    relative: str,
) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except OSError as exc:
        raise WorkspaceSnapshotRaceError(
            f"Workspace snapshot entry disappeared: {relative}"
        ) from exc


def _read_snapshot_symlink_at(
    directory_descriptor: int,
    name: str,
    relative: str,
    entry_stat: os.stat_result,
) -> bytes:
    try:
        target = os.readlink(name, dir_fd=directory_descriptor)
        current_stat = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise WorkspaceSnapshotRaceError(
            f"Workspace snapshot symlink changed: {relative}"
        ) from exc
    if not stat.S_ISLNK(current_stat.st_mode) or not _same_snapshot_entry(
        entry_stat,
        current_stat,
    ):
        raise WorkspaceSnapshotRaceError(
            f"Workspace snapshot symlink changed: {relative}"
        )
    return target.encode("utf-8")


def _open_snapshot_directory(
    path: Path,
    entry_stat: os.stat_result,
    relative: str,
) -> int:
    return _open_snapshot_directory_target(path, None, entry_stat, relative)


def _open_snapshot_directory_at(
    directory_descriptor: int,
    name: str,
    entry_stat: os.stat_result,
    relative: str,
) -> int:
    return _open_snapshot_directory_target(
        name,
        directory_descriptor,
        entry_stat,
        relative,
    )


def _open_snapshot_directory_target(
    target: str | Path,
    directory_descriptor: int | None,
    entry_stat: os.stat_result,
    relative: str,
) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(target, flags, dir_fd=directory_descriptor)
    except OSError as exc:
        raise WorkspaceSnapshotRaceError(
            f"Workspace snapshot directory changed before open: {relative}"
        ) from exc
    opened_stat = os.fstat(descriptor)
    if not stat.S_ISDIR(opened_stat.st_mode) or not _same_snapshot_entry(
        entry_stat,
        opened_stat,
    ):
        os.close(descriptor)
        raise WorkspaceSnapshotRaceError(
            f"Workspace snapshot directory changed type or identity: {relative}"
        )
    return descriptor


def _same_snapshot_entry(
    first: os.stat_result,
    second: os.stat_result,
) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _unsupported_snapshot_entry(
    relative: str,
    mode: int,
) -> WorkspaceSnapshotEntryError:
    return WorkspaceSnapshotEntryError(
        "Workspace snapshots support only directories, regular files, and "
        f"symlinks; rejected '{relative}' with mode {stat.S_IFMT(mode):#o}."
    )


def _count_snapshot_entry(
    budget: _WorkspaceSnapshotBudget,
    relative: str,
) -> None:
    _check_snapshot_budget(budget)
    budget.entry_count += 1
    if budget.entry_count > budget.policy.max_entries:
        raise WorkspaceSnapshotLimitError(
            "Workspace snapshot entry limit exceeded "
            f"at '{relative}' ({budget.policy.max_entries})."
        )


def _reserve_snapshot_file_bytes(
    budget: _WorkspaceSnapshotBudget,
    size_bytes: int,
    relative: str,
) -> None:
    budget.file_bytes += size_bytes
    if budget.file_bytes > budget.policy.max_file_bytes:
        raise WorkspaceSnapshotLimitError(
            "Workspace snapshot byte limit exceeded "
            f"at '{relative}' ({budget.policy.max_file_bytes})."
        )


def _check_snapshot_budget(budget: _WorkspaceSnapshotBudget) -> None:
    if budget.policy.cancel_requested is not None and budget.policy.cancel_requested():
        raise WorkspaceSnapshotCancelled("Workspace snapshot was cancelled.")
    elapsed = budget.policy.clock() - budget.started_at
    if elapsed > budget.policy.max_elapsed_seconds:
        raise WorkspaceSnapshotLimitError(
            "Workspace snapshot elapsed-time limit exceeded "
            f"({budget.policy.max_elapsed_seconds:g}s)."
        )
