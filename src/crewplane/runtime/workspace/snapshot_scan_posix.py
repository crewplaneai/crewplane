from __future__ import annotations

import os
import stat
from pathlib import Path

from .snapshot_scan_common import (
    WorkspaceSnapshotBudget,
    WorkspaceSnapshotEntryError,
    WorkspaceSnapshotError,
    WorkspaceSnapshotRaceError,
    check_snapshot_budget,
    count_snapshot_entry,
    hash_open_snapshot_file,
    same_snapshot_entry,
    snapshot_entry_digest,
    unsupported_snapshot_entry,
)


def scan_snapshot(root: Path, budget: WorkspaceSnapshotBudget) -> dict[str, str]:
    root_stat = _require_snapshot_directory(root)
    entries: dict[str, str] = {}
    root_descriptor = _open_snapshot_directory(root, root_stat, ".")
    try:
        _scan_snapshot_directory(budget, root_descriptor, "", entries)
    finally:
        os.close(root_descriptor)
    return entries


def _scan_snapshot_directory(
    budget: WorkspaceSnapshotBudget,
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
            entries[relative] = snapshot_entry_digest(
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
                entries[relative] = snapshot_entry_digest(
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
            raise unsupported_snapshot_entry(relative, mode)
        entries[relative] = _snapshot_regular_file_digest(
            budget,
            directory_descriptor,
            name,
            relative,
            entry_stat,
        )


def _discover_snapshot_entries(
    budget: WorkspaceSnapshotBudget,
    directory_descriptor: int,
    relative_parent: str,
) -> list[tuple[str, str, os.stat_result]]:
    check_snapshot_budget(budget)
    discovered: list[tuple[str, str, os.stat_result]] = []
    try:
        with os.scandir(directory_descriptor) as iterator:
            for entry in iterator:
                relative = (
                    f"{relative_parent}/{entry.name}" if relative_parent else entry.name
                )
                if relative in budget.policy.excluded_roots:
                    continue
                count_snapshot_entry(budget, relative)
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
    budget: WorkspaceSnapshotBudget,
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
        return hash_open_snapshot_file(budget, descriptor, relative, opened_stat)
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
    if not stat.S_ISLNK(current_stat.st_mode) or not same_snapshot_entry(
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
    if not stat.S_ISDIR(opened_stat.st_mode) or not same_snapshot_entry(
        entry_stat,
        opened_stat,
    ):
        os.close(descriptor)
        raise WorkspaceSnapshotRaceError(
            f"Workspace snapshot directory changed type or identity: {relative}"
        )
    return descriptor
