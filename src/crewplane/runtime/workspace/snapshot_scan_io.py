"""Windows directory enumeration and protected entry reads for observations."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from crewplane.architecture.safe_file_reads import stable_file_signature
from crewplane.architecture.safe_files_windows import (
    open_regular_file,
    protected_directory,
)
from crewplane.architecture.windows_file_handles import FileHandle

from . import snapshot_scan as scan


def scan_windows_directory(
    root: Path,
    relative_parent: str,
    entries: dict[str, str],
    budget: scan.WorkspaceSnapshotBudget,
) -> None:
    directory = root / relative_parent
    try:
        with protected_directory(directory, list_entries=True) as handle:
            discovered = _discover_entries(handle, relative_parent, budget)
            for relative, metadata in sorted(discovered):
                path = root / relative
                if getattr(metadata, "st_file_attributes", 0) & 0x400:
                    raise scan.WorkspaceSnapshotEntryError(
                        f"Reparse redirection is unsupported in observations: {relative}"
                    )
                if stat.S_ISDIR(metadata.st_mode):
                    with protected_directory(path):
                        if not scan.same_snapshot_entry(metadata, path.lstat()):
                            raise scan.WorkspaceSnapshotRaceError(
                                f"Workspace snapshot directory changed: {relative}"
                            )
                        entries[relative] = scan.snapshot_entry_digest(
                            relative, metadata, "dir", b""
                        )
                        scan_windows_directory(root, relative, entries, budget)
                elif stat.S_ISREG(metadata.st_mode):
                    entries[relative] = _file_digest(path, relative, metadata, budget)
                else:
                    raise scan.unsupported_snapshot_entry(relative, metadata.st_mode)
    except (OSError, ValueError) as exc:
        raise scan.WorkspaceSnapshotRaceError(
            f"Workspace observation could not safely read {directory}: {exc}"
        ) from exc


def _discover_entries(
    directory: FileHandle, parent: str, budget: scan.WorkspaceSnapshotBudget
) -> list[tuple[str, os.stat_result]]:
    scan.check_snapshot_budget(budget)
    excluded = {value.casefold() for value in budget.policy.excluded_roots}
    entries = []
    for name in directory.entry_names():
        relative = f"{parent}/{name}" if parent else name
        if relative.casefold() in excluded:
            continue
        scan.count_snapshot_entry(budget, relative)
        native = directory.open_child(name)
        try:
            metadata = native.information()
            native.validate(directory=bool(metadata.attributes & 0x10))
            descriptor = native.into_descriptor()
            try:
                entries.append((relative, os.fstat(descriptor)))
            finally:
                os.close(descriptor)
        finally:
            native.close()
    return entries


def _file_digest(
    path: Path,
    relative: str,
    metadata: os.stat_result,
    budget: scan.WorkspaceSnapshotBudget,
) -> str:
    with open_regular_file(path) as descriptor:
        opened = os.fstat(descriptor)
        if stable_file_signature(metadata) != stable_file_signature(opened):
            raise scan.WorkspaceSnapshotRaceError(
                f"Workspace snapshot file changed before reading: {relative}"
            )
        result = scan.hash_open_snapshot_file(budget, descriptor, relative, opened)
        if stable_file_signature(opened) != stable_file_signature(os.fstat(descriptor)):
            raise scan.WorkspaceSnapshotRaceError(
                f"Workspace snapshot file changed while reading: {relative}"
            )
        return result
