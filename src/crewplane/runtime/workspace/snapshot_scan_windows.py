"""Windows directory enumeration and protected entry reads for observations."""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING

from crewplane.architecture.safe_file_reads import stable_file_signature
from crewplane.architecture.safe_files_windows import (
    open_regular_file,
    protected_directory,
)
from crewplane.architecture.windows_file_handles import (
    FILE_ATTRIBUTE_DIRECTORY,
    FILE_ATTRIBUTE_REPARSE_POINT,
)

from . import snapshot_scan_common as scan

if TYPE_CHECKING:
    from pathlib import Path

    from crewplane.architecture.windows_file_handles import FileHandle


def scan_windows_directory(
    root: Path,
    relative_parent: str,
    entries: dict[str, str],
    budget: scan.WorkspaceSnapshotBudget,
) -> None:
    """Recursively add fingerprints from a protected Windows directory.

    Discover and count children before processing them in sorted, depth-first
    order. Match excluded roots case-insensitively and keep directory protection
    active throughout descendant traversal. Mutate entries and budget in place;
    recorded fingerprints and consumed budget remain if scanning fails.

    Args:
        root: Workspace root used to resolve relative paths.
        relative_parent: Directory to scan relative to root, or an empty string
            to scan root itself.
        entries: Mapping to receive directory and regular-file fingerprints.
        budget: Shared resource limits and counters for the recursive scan.

    Raises:
        scan.WorkspaceSnapshotError: An unsupported entry, detected race,
            resource limit, or cancellation; propagated unchanged.
        scan.WorkspaceSnapshotRaceError: An OSError or ValueError from protected
            I/O, validation, or traversal; translated with the original cause.
    """
    directory = root / relative_parent
    try:
        with protected_directory(directory, list_entries=True) as handle:
            discovered = _discover_entries(handle, relative_parent, budget)
            for relative, metadata in sorted(discovered):
                _scan_entry(root, relative, metadata, entries, budget)
    except (OSError, ValueError) as exc:
        raise scan.WorkspaceSnapshotRaceError(
            f"Workspace observation could not safely read {directory}: {exc}"
        ) from exc


def _scan_entry(
    root: Path,
    relative: str,
    metadata: os.stat_result,
    entries: dict[str, str],
    budget: scan.WorkspaceSnapshotBudget,
) -> None:
    if getattr(metadata, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
        raise scan.WorkspaceSnapshotEntryError(
            f"Reparse redirection is unsupported in observations: {relative}"
        )
    if stat.S_ISDIR(metadata.st_mode):
        _scan_directory_entry(root, relative, metadata, entries, budget)
        return
    if not stat.S_ISREG(metadata.st_mode):
        raise scan.unsupported_snapshot_entry(relative, metadata.st_mode)
    entries[relative] = _file_digest(root / relative, relative, metadata, budget)


def _scan_directory_entry(
    root: Path,
    relative: str,
    metadata: os.stat_result,
    entries: dict[str, str],
    budget: scan.WorkspaceSnapshotBudget,
) -> None:
    path = root / relative
    with protected_directory(path):
        if not scan.same_snapshot_entry(metadata, path.lstat()):
            raise scan.WorkspaceSnapshotRaceError(
                f"Workspace snapshot directory changed: {relative}"
            )
        entries[relative] = scan.snapshot_entry_digest(relative, metadata, "dir", b"")
        scan_windows_directory(root, relative, entries, budget)


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
            native.validate(
                directory=bool(metadata.attributes & FILE_ATTRIBUTE_DIRECTORY)
            )
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


def scan_snapshot(root: Path, budget: scan.WorkspaceSnapshotBudget) -> dict[str, str]:
    entries: dict[str, str] = {}
    scan_windows_directory(root, "", entries, budget)
    return entries
