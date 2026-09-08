"""Capture safe filesystem snapshots for drift detection."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator
from pathlib import Path

from crewplane.architecture.ports import ArtifactStorePort
from crewplane.core.file_hashing import ContentSignature

from ..types import DirectorySnapshot


def file_snapshot_signature(file_path: Path) -> ContentSignature:
    file_stat = _single_link_regular_file_stat(file_path)
    payload = _read_single_link_regular_file(file_path, file_stat)
    return len(payload), hashlib.sha256(payload).hexdigest()


def snapshot_files(
    root: Path,
    excluded_paths: set[Path] | None = None,
    excluded_roots: set[Path] | None = None,
) -> dict[Path, ContentSignature]:
    excluded = excluded_paths or set()
    roots = excluded_roots or set()
    root_stat = _snapshot_root_stat(root)
    if root_stat is None:
        return {}
    if stat.S_ISREG(root_stat.st_mode):
        return _snapshot_regular_file_root(root, root_stat, excluded, roots)
    if not stat.S_ISDIR(root_stat.st_mode):
        return {}
    return _snapshot_non_directory_entries(root, excluded, roots)


def snapshot_file_bytes(
    root: Path,
    excluded_paths: set[Path] | None = None,
    excluded_roots: set[Path] | None = None,
) -> dict[Path, bytes]:
    contents: dict[Path, bytes] = {}
    for path in snapshot_files(
        root,
        excluded_paths=excluded_paths,
        excluded_roots=excluded_roots,
    ):
        path_stat = lstat_or_none(path)
        if path_stat is None or not is_single_link_regular_file(path_stat):
            continue
        try:
            contents[path] = _read_single_link_regular_file(path, path_stat)
        except FileNotFoundError:
            continue
    return contents


def snapshot_directories(
    root: Path,
    excluded_paths: set[Path] | None = None,
    excluded_roots: set[Path] | None = None,
) -> dict[Path, DirectorySnapshot]:
    """Record real directory entries so file/directory substitutions are visible."""

    excluded = excluded_paths or set()
    roots = excluded_roots or set()
    root_stat = _snapshot_root_stat(root)
    if root_stat is None:
        return {}
    if not stat.S_ISDIR(root_stat.st_mode):
        return {}
    return _snapshot_directory_tree(root, root_stat, excluded, roots)


def _snapshot_root_stat(root: Path) -> os.stat_result | None:
    root_stat = lstat_or_none(root)
    if root_stat is not None and stat.S_ISLNK(root_stat.st_mode):
        raise RuntimeError(f"Drift snapshot root must not be a symlink: {root}")
    return root_stat


def _snapshot_regular_file_root(
    root: Path,
    root_stat: os.stat_result,
    excluded_paths: set[Path],
    excluded_roots: set[Path],
) -> dict[Path, ContentSignature]:
    if _snapshot_path_is_excluded(root, excluded_paths, excluded_roots):
        return {}
    try:
        return {root: _snapshot_signature(root, root_stat)}
    except FileNotFoundError:
        return {}


def _snapshot_non_directory_entries(
    root: Path,
    excluded_paths: set[Path],
    excluded_roots: set[Path],
) -> dict[Path, ContentSignature]:
    snapshot: dict[Path, ContentSignature] = {}
    for entry_path, entry_stat in _iter_included_descendants(
        root,
        excluded_paths,
        excluded_roots,
    ):
        if stat.S_ISDIR(entry_stat.st_mode):
            continue
        try:
            snapshot[entry_path] = _snapshot_signature(entry_path, entry_stat)
        except FileNotFoundError:
            continue
    return snapshot


def _snapshot_directory_tree(
    root: Path,
    root_stat: os.stat_result,
    excluded_paths: set[Path],
    excluded_roots: set[Path],
) -> dict[Path, DirectorySnapshot]:
    snapshot = {root: _directory_snapshot(root, root_stat)}
    for entry_path, entry_stat in _iter_included_descendants(
        root,
        excluded_paths,
        excluded_roots,
    ):
        if not stat.S_ISDIR(entry_stat.st_mode):
            continue
        try:
            snapshot[entry_path] = _directory_snapshot(
                entry_path,
                entry_stat,
            )
        except FileNotFoundError:
            continue
    return snapshot


def _iter_included_descendants(
    root: Path,
    excluded_paths: set[Path],
    excluded_roots: set[Path],
) -> Iterator[tuple[Path, os.stat_result]]:
    for entry_path in root.rglob("*"):
        if _snapshot_path_is_excluded(
            entry_path,
            excluded_paths,
            excluded_roots,
        ):
            continue
        entry_stat = lstat_or_none(entry_path)
        if entry_stat is not None:
            yield entry_path, entry_stat


def _snapshot_path_is_excluded(
    path: Path,
    excluded_paths: set[Path],
    excluded_roots: set[Path],
) -> bool:
    return path in excluded_paths or snapshot_path_is_within_roots(
        path,
        excluded_roots,
    )


def snapshot_path_is_within_roots(path: Path, roots: set[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def signatures_for_bytes(
    content_by_path: dict[Path, bytes],
) -> dict[Path, ContentSignature]:
    return {
        path: (len(content), hashlib.sha256(content).hexdigest())
        for path, content in content_by_path.items()
    }


def read_file_bytes(file_path: Path) -> bytes | None:
    file_stat = lstat_or_none(file_path)
    if file_stat is None:
        return None
    return _read_single_link_regular_file(file_path, file_stat)


def lstat_or_none(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def is_single_link_regular_file(file_stat: os.stat_result) -> bool:
    return stat.S_ISREG(file_stat.st_mode) and file_stat.st_nlink == 1


def _single_link_regular_file_stat(path: Path) -> os.stat_result:
    file_stat = lstat_or_none(path)
    if file_stat is None:
        raise FileNotFoundError(path)
    if not is_single_link_regular_file(file_stat):
        raise ValueError(f"Drift snapshots require single-link regular files: {path}")
    return file_stat


def _read_single_link_regular_file(
    path: Path,
    file_stat: os.stat_result,
) -> bytes:
    if not is_single_link_regular_file(file_stat):
        raise RuntimeError(f"Drift snapshot path is not a safe regular file: {path}")
    payload = path.read_bytes()
    current_stat = _single_link_regular_file_stat(path)
    if (
        current_stat.st_dev != file_stat.st_dev
        or current_stat.st_ino != file_stat.st_ino
        or current_stat.st_size != file_stat.st_size
    ):
        raise RuntimeError(f"Drift snapshot path changed during read: {path}")
    return payload


def _snapshot_signature(
    path: Path,
    path_stat: os.stat_result,
) -> ContentSignature:
    if is_single_link_regular_file(path_stat):
        return file_snapshot_signature(path)
    target = os.readlink(path) if stat.S_ISLNK(path_stat.st_mode) else ""
    descriptor = ":".join(
        str(value)
        for value in (
            path_stat.st_mode,
            path_stat.st_dev,
            path_stat.st_ino,
            path_stat.st_nlink,
            path_stat.st_size,
            path_stat.st_mtime_ns,
            target,
        )
    ).encode("utf-8")
    return len(descriptor), hashlib.sha256(descriptor).hexdigest()


def _directory_snapshot(
    path: Path,
    path_stat: os.stat_result,
) -> DirectorySnapshot:
    with os.scandir(path) as entries:
        entry_names = tuple(sorted(entry.name for entry in entries))
    return DirectorySnapshot(
        mode=path_stat.st_mode,
        device=path_stat.st_dev,
        inode=path_stat.st_ino,
        user_id=path_stat.st_uid,
        group_id=path_stat.st_gid,
        link_count=path_stat.st_nlink,
        changed_at_ns=path_stat.st_ctime_ns,
        entry_names=entry_names,
    )


def manifests_dir_for(output: ArtifactStorePort) -> Path:
    return output.stages_dir / "manifests"
