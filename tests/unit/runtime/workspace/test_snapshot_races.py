from __future__ import annotations

import os
from pathlib import Path
from typing import IO
from unittest.mock import patch

import pytest

from crewplane.runtime.workspace.filesystem import (
    ensure_owner_private_dir,
    remove_workspace_path,
)
from crewplane.runtime.workspace.snapshot import (
    WorkspaceSnapshotEntryError,
    WorkspaceSnapshotPolicy,
    WorkspaceSnapshotRaceError,
    snapshot_digest,
    snapshot_entries,
    workspace_directory_identity,
)


@pytest.mark.parametrize(
    ("kind", "error_type", "message"),
    [
        ("missing", WorkspaceSnapshotRaceError, "root is unavailable"),
        ("file", WorkspaceSnapshotEntryError, "root is not a directory"),
        ("symlink", WorkspaceSnapshotEntryError, "root is not a directory"),
    ],
    ids=["missing", "file", "symlink"],
)
def test_snapshot_rejects_unavailable_or_unsafe_root(
    tmp_path: Path, kind: str, error_type: type[RuntimeError], message: str
) -> None:
    root = tmp_path / "root"
    if kind == "file":
        root.write_bytes(b"keep")
    elif kind == "symlink":
        root.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(error_type, match=message):
        snapshot_entries(root)
    with pytest.raises(RuntimeError, match="missing|not a real directory"):
        workspace_directory_identity(root)


@pytest.mark.parametrize("kind", ["file", "dangling-symlink", "directory-symlink"])
def test_private_cache_directory_rejects_unowned_entries(
    tmp_path: Path, kind: str
) -> None:
    root = tmp_path / "cache"
    if kind == "file":
        root.write_bytes(b"keep")
    else:
        root.symlink_to(
            tmp_path if kind == "directory-symlink" else tmp_path / "missing"
        )
    with pytest.raises(RuntimeError, match="not a directory|must not be a symlink"):
        ensure_owner_private_dir(root)


@pytest.mark.parametrize(
    ("max_entries", "max_file_bytes", "max_elapsed_seconds"),
    [
        (0, 1, 1.0),
        (1, -1, 1.0),
        (1, 1, 0.0),
        (1, 1, float("inf")),
        (1, 1, float("nan")),
    ],
)
def test_snapshot_budget_rejects_invalid_limits(
    max_entries: int,
    max_file_bytes: int,
    max_elapsed_seconds: float,
) -> None:
    with pytest.raises(ValueError, match="must be positive|must be nonnegative"):
        WorkspaceSnapshotPolicy(max_entries, max_file_bytes, max_elapsed_seconds)


@pytest.mark.parametrize("kind", ["file", "directory"])
@pytest.mark.parametrize("change", ["remove", "replace"])
def test_snapshot_detects_entry_changed_between_discovery_and_open(
    tmp_path: Path, kind: str, change: str
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    entry = root / "entry"
    if kind == "file":
        entry.write_bytes(b"original")
    else:
        entry.mkdir()
    real_open = os.open
    changed = False

    def open_after_change(
        path: str | Path, flags: int, mode: int = 0o600, dir_fd: int | None = None
    ) -> int:
        nonlocal changed
        if path == "entry" and not changed:
            changed = True
            entry.rename(tmp_path / "original")
            if change == "replace":
                if kind == "file":
                    entry.write_bytes(b"replacement")
                else:
                    entry.mkdir()
        return real_open(path, flags, mode, dir_fd=dir_fd)

    with (
        patch.object(os, "open", new=open_after_change),
        pytest.raises(WorkspaceSnapshotRaceError, match="before open|type or identity"),
    ):
        snapshot_entries(root)
    assert changed
    assert (tmp_path / "original").exists()


@pytest.mark.parametrize("change", ["remove", "replace"])
def test_snapshot_detects_symlink_changed_during_read(
    tmp_path: Path, change: str
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    link = root / "link"
    link.symlink_to("original-target")
    real_readlink = os.readlink

    def readlink_and_change(path: str, dir_fd: int | None = None) -> str:
        target = real_readlink(path, dir_fd=dir_fd)
        link.rename(tmp_path / "original-link")
        if change == "replace":
            link.symlink_to("different-target")
        return target

    with (
        patch.object(os, "readlink", new=readlink_and_change),
        pytest.raises(WorkspaceSnapshotRaceError, match="symlink changed"),
    ):
        snapshot_entries(root)
    assert os.readlink(tmp_path / "original-link") == "original-target"


@pytest.mark.parametrize("payload", [b"", b"longer-than-original"])
def test_snapshot_detects_file_size_changed_while_hashing(
    tmp_path: Path, payload: bytes
) -> None:
    file = tmp_path / "file"
    file.write_bytes(b"original")
    real_fdopen = os.fdopen

    def open_changed_file(descriptor: int, mode: str, closefd: bool) -> IO[bytes]:
        file.write_bytes(payload)
        return real_fdopen(descriptor, mode, closefd=closefd)

    with (
        patch.object(os, "fdopen", new=open_changed_file),
        pytest.raises(
            WorkspaceSnapshotRaceError,
            match="grew while hashing|changed size while hashing",
        ),
    ):
        snapshot_entries(tmp_path)


def test_snapshot_reports_directory_scan_failure(tmp_path: Path) -> None:
    with (
        patch.object(os, "scandir", side_effect=OSError("directory disappeared")),
        pytest.raises(
            WorkspaceSnapshotRaceError, match="directory changed while scanning"
        ),
    ):
        snapshot_entries(tmp_path)


def test_snapshot_reports_entry_disappearance_during_discovery(tmp_path: Path) -> None:
    entry = tmp_path / "entry"
    entry.write_bytes(b"original")
    real_stat = os.stat

    def stat_after_unlink(
        path: str | Path, dir_fd: int | None = None, follow_symlinks: bool = True
    ) -> os.stat_result:
        if path == "entry" and dir_fd is not None:
            entry.unlink()
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    with (
        patch.object(os, "stat", new=stat_after_unlink),
        pytest.raises(WorkspaceSnapshotRaceError, match="entry disappeared"),
    ):
        snapshot_entries(tmp_path)
    assert not entry.exists()


def test_snapshot_digest_tracks_permissions_and_nested_content(tmp_path: Path) -> None:
    (tmp_path / "nested").mkdir()
    file = tmp_path / "nested" / "file"
    file.write_bytes(b"original")
    initial = snapshot_digest(tmp_path)
    assert snapshot_digest(tmp_path) == initial
    file.chmod(0o700)
    assert snapshot_digest(tmp_path) != initial
    executable = snapshot_digest(tmp_path)
    file.write_bytes(b"modified")
    assert snapshot_digest(tmp_path) != executable


def test_cleanup_unlinks_symlink_without_following_directory_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep").write_bytes(b"trusted")
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    remove_workspace_path(link)
    remove_workspace_path(link)
    assert not link.is_symlink()
    assert (target / "keep").read_bytes() == b"trusted"
