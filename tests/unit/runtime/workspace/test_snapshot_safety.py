from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.runtime.workspace.snapshot import (
    WorkspaceSnapshotCancelled,
    WorkspaceSnapshotEntryError,
    WorkspaceSnapshotLimitError,
    WorkspaceSnapshotPolicy,
    WorkspaceSnapshotRaceError,
    snapshot_entries,
)


@pytest.mark.skipif(os.name != "posix", reason="special-file checks are POSIX-only")
def test_snapshot_entries_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "provider.pipe")

    with pytest.raises(WorkspaceSnapshotEntryError, match="provider.pipe"):
        snapshot_entries(tmp_path)


def test_snapshot_entries_enforces_entry_and_byte_limits(tmp_path: Path) -> None:
    (tmp_path / "first.txt").write_bytes(b"1234")
    (tmp_path / "second.txt").write_bytes(b"5678")

    with pytest.raises(WorkspaceSnapshotLimitError, match="entry limit"):
        snapshot_entries(
            tmp_path,
            WorkspaceSnapshotPolicy(max_entries=1),
        )
    with pytest.raises(WorkspaceSnapshotLimitError, match="byte limit"):
        snapshot_entries(
            tmp_path,
            WorkspaceSnapshotPolicy(max_file_bytes=7),
        )


def test_snapshot_entries_enforces_elapsed_time_and_cancellation(
    tmp_path: Path,
) -> None:
    (tmp_path / "payload.txt").write_text("payload", encoding="utf-8")
    clock_values = iter((0.0, 0.0, 2.0))

    with pytest.raises(WorkspaceSnapshotLimitError, match="elapsed-time"):
        snapshot_entries(
            tmp_path,
            WorkspaceSnapshotPolicy(
                max_elapsed_seconds=1.0,
                clock=lambda: next(clock_values),
            ),
        )
    with pytest.raises(WorkspaceSnapshotCancelled):
        snapshot_entries(
            tmp_path,
            WorkspaceSnapshotPolicy(cancel_requested=lambda: True),
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_snapshot_policy_rejects_nonfinite_elapsed_limits(value: float) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        WorkspaceSnapshotPolicy(max_elapsed_seconds=value)


@pytest.mark.skipif(os.name != "posix", reason="no-follow open checks are POSIX-only")
def test_snapshot_entries_rejects_file_type_change_before_open(
    tmp_path: Path,
) -> None:
    path = tmp_path / "payload.txt"
    path.write_text("payload", encoding="utf-8")
    real_open = os.open

    def replace_with_fifo(
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        dir_fd: int | None = None,
    ) -> int:
        if target == path.name:
            path.unlink()
            os.mkfifo(path)
        return real_open(target, flags, mode, dir_fd=dir_fd)

    with (
        patch(
            "crewplane.runtime.workspace.snapshot.os.open",
            new=replace_with_fifo,
        ),
        pytest.raises(WorkspaceSnapshotRaceError, match="changed type"),
    ):
        snapshot_entries(tmp_path)


@pytest.mark.skipif(os.name != "posix", reason="no-follow open checks are POSIX-only")
def test_snapshot_entries_rejects_directory_swap_before_descent(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "nested"
    directory.mkdir()
    (directory / "inside.txt").write_text("inside", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "ambient.txt").write_text("ambient", encoding="utf-8")
    real_open = os.open

    def replace_with_symlink(
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        dir_fd: int | None = None,
    ) -> int:
        if target == directory.name:
            (directory / "inside.txt").unlink()
            directory.rmdir()
            directory.symlink_to(outside, target_is_directory=True)
        return real_open(target, flags, mode, dir_fd=dir_fd)

    with (
        patch(
            "crewplane.runtime.workspace.snapshot.os.open",
            new=replace_with_symlink,
        ),
        pytest.raises(WorkspaceSnapshotRaceError, match="changed before open"),
    ):
        snapshot_entries(tmp_path)
