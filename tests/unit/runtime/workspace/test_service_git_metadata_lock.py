from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

import crewplane.runtime.workspace.locks as workspace_locks


def test_git_metadata_lock_fails_explicitly_without_posix_fcntl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workspace_locks, "fcntl", None)

    with (
        pytest.raises(RuntimeError, match="POSIX fcntl"),
        workspace_locks.git_metadata_lock(tmp_path),
    ):
        pass

    assert not (tmp_path / "crewplane").exists()


def test_git_metadata_lock_is_reentrant_for_same_repository(tmp_path: Path) -> None:
    with (
        workspace_locks.git_metadata_lock(tmp_path),
        workspace_locks.git_metadata_lock(tmp_path),
    ):
        assert (tmp_path / "crewplane" / "workspace.lock").exists()


def test_git_metadata_lock_cancellation_stops_in_process_lock_wait(
    tmp_path: Path,
) -> None:
    locked = Event()
    release = Event()

    def hold_lock() -> None:
        with workspace_locks.git_metadata_lock(tmp_path):
            locked.set()
            assert release.wait(2)

    with ThreadPoolExecutor(max_workers=1) as executor:
        holder = executor.submit(hold_lock)
        assert locked.wait(2)
        try:
            with (
                pytest.raises(RuntimeError, match="lock acquisition was cancelled"),
                workspace_locks.git_metadata_lock(tmp_path, lambda: True),
            ):
                pass
        finally:
            release.set()
        holder.result()


def test_git_metadata_lock_releases_immediate_cancelled_acquisition(
    tmp_path: Path,
) -> None:
    with (
        pytest.raises(RuntimeError, match="lock acquisition was cancelled"),
        workspace_locks.git_metadata_lock(tmp_path, lambda: True),
    ):
        pass

    with workspace_locks.git_metadata_lock(tmp_path):
        assert (tmp_path / "crewplane" / "workspace.lock").exists()


def test_git_metadata_lock_releases_file_lock_when_cancelled_after_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_lock_api = workspace_locks.fcntl
    if file_lock_api is None:
        pytest.skip("POSIX fcntl is unavailable")
    operations: list[int] = []

    def record_file_lock(file_descriptor: int, operation: int) -> None:
        del file_descriptor
        operations.append(operation)

    def cancel_after_file_lock_acquisition() -> bool:
        return bool(operations)

    monkeypatch.setattr(file_lock_api, "flock", record_file_lock)

    with (
        pytest.raises(RuntimeError, match="lock acquisition was cancelled"),
        workspace_locks.git_metadata_lock(
            tmp_path,
            cancel_after_file_lock_acquisition,
        ),
    ):
        pass

    assert operations == [
        file_lock_api.LOCK_EX | file_lock_api.LOCK_NB,
        file_lock_api.LOCK_UN,
    ]
