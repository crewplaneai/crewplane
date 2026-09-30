from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.runtime.workspace.snapshot_scan import (
    WorkspaceSnapshotCancelled,
    WorkspaceSnapshotEntryError,
    WorkspaceSnapshotLimitError,
    WorkspaceSnapshotPolicy,
    WorkspaceSnapshotRaceError,
    snapshot_digest,
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
        dir_fd: int | None = None,
    ) -> int:
        if target == path.name:
            path.unlink()
            os.mkfifo(path)
        return real_open(target, flags, dir_fd=dir_fd)

    with (
        patch(
            "crewplane.runtime.workspace.snapshot_scan.os.open",
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
        dir_fd: int | None = None,
    ) -> int:
        if target == directory.name:
            (directory / "inside.txt").unlink()
            directory.rmdir()
            directory.symlink_to(outside, target_is_directory=True)
        return real_open(target, flags, dir_fd=dir_fd)

    with (
        patch(
            "crewplane.runtime.workspace.snapshot_scan.os.open",
            new=replace_with_symlink,
        ),
        pytest.raises(WorkspaceSnapshotRaceError, match="changed before open"),
    ):
        snapshot_entries(tmp_path)


def test_snapshot_fingerprints_preserve_encoding_order_and_exclusions(
    tmp_path: Path,
) -> None:
    (tmp_path / "z.txt").write_bytes(b"payload\x00\xff")
    (tmp_path / "z.txt").chmod(0o640)
    (tmp_path / "nested").mkdir(mode=0o750)
    (tmp_path / "nested").chmod(0o750)
    (tmp_path / "nested" / "empty").write_bytes(b"")
    (tmp_path / "nested" / "empty").chmod(0o600)
    (tmp_path / "link").symlink_to("z.txt")
    link_mode = stat.S_IMODE((tmp_path / "link").lstat().st_mode)
    expected = {
        "link": hashlib.sha256(
            f"symlink\0link\0{link_mode:o}\0z.txt".encode()
        ).hexdigest(),
        "nested": hashlib.sha256(b"dir\0nested\x00750\0").hexdigest(),
        "nested/empty": hashlib.sha256(b"file\0nested/empty\x00600\0").hexdigest(),
        "z.txt": hashlib.sha256(b"file\0z.txt\x00640\0payload\x00\xff").hexdigest(),
    }
    entries = snapshot_entries(tmp_path)
    assert entries == expected
    assert list(entries) == list(expected)
    encoded = "".join(
        f"{path}\0{digest}\0" for path, digest in expected.items()
    ).encode()
    assert snapshot_digest(tmp_path) == hashlib.sha256(encoded).hexdigest()
    assert snapshot_entries(
        tmp_path, WorkspaceSnapshotPolicy(excluded_roots=frozenset({"nested"}))
    ) == {key: expected[key] for key in ("link", "z.txt")}


def test_snapshot_accepts_exact_entry_byte_and_elapsed_limits(tmp_path: Path) -> None:
    (tmp_path / "file").write_bytes(b"1234")
    ticks = iter([0.0, 5.0, 5.0, 5.0])
    assert list(
        snapshot_entries(
            tmp_path,
            WorkspaceSnapshotPolicy(1, 4, 5.0, clock=ticks.__next__),
        )
    ) == ["file"]


def test_snapshot_default_resource_limits_remain_unchanged() -> None:
    policy = WorkspaceSnapshotPolicy()
    assert (policy.max_entries, policy.max_file_bytes, policy.max_elapsed_seconds) == (
        250_000,
        4 * 1024 * 1024 * 1024,
        30.0,
    )


@pytest.mark.parametrize("byte_limit", [0, 4])
def test_snapshot_closes_owned_descriptors_after_success_and_failure(
    tmp_path: Path,
    byte_limit: int,
) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "file").write_bytes(b"1234")
    real_close = os.close
    with patch(
        "crewplane.runtime.workspace.snapshot_scan.os.close", wraps=real_close
    ) as close:
        if byte_limit:
            snapshot_entries(
                tmp_path, WorkspaceSnapshotPolicy(max_file_bytes=byte_limit)
            )
        else:
            with pytest.raises(WorkspaceSnapshotLimitError, match="byte limit"):
                snapshot_entries(
                    tmp_path, WorkspaceSnapshotPolicy(max_file_bytes=byte_limit)
                )
    descriptors = [call.args[0] for call in close.call_args_list]
    assert len(descriptors) == len(set(descriptors)) == 3
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)
