from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock, RLock, local
from time import sleep
from typing import BinaryIO, Protocol

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on native Windows.
    fcntl = None  # type: ignore[assignment]


_LOCKS_GUARD = Lock()
_REPOSITORY_LOCKS: dict[Path, RLock] = {}
_THREAD_LOCK_DEPTHS = local()
_LOCK_CANCELLATION_POLL_SECONDS = 0.01


class _FileLockApi(Protocol):
    LOCK_EX: int
    LOCK_NB: int
    LOCK_UN: int

    def flock(self, file_descriptor: int, operation: int) -> None: ...


@contextmanager
def git_metadata_lock(
    common_git_dir: Path,
    cancel_requested: Callable[[], bool] | None = None,
) -> Iterator[None]:
    _file_lock_api()
    repository_key = common_git_dir.resolve(strict=False)
    repository_lock = _repository_lock(repository_key)
    with _held_repository_lock(repository_lock, cancel_requested):
        if _lock_depth(repository_key) > 0:
            with _tracked_lock_depth(repository_key):
                yield
            return

        lock_path = common_git_dir / "crewplane" / "workspace.lock"
        with (
            _held_file_lock(lock_path, cancel_requested),
            _tracked_lock_depth(repository_key),
        ):
            yield


@contextmanager
def _held_repository_lock(
    repository_lock: RLock,
    cancel_requested: Callable[[], bool] | None,
) -> Iterator[None]:
    _acquire_repository_lock(repository_lock, cancel_requested)
    try:
        _raise_if_lock_cancelled(cancel_requested)
        yield
    finally:
        repository_lock.release()


@contextmanager
def _held_file_lock(
    lock_path: Path,
    cancel_requested: Callable[[], bool] | None,
) -> Iterator[None]:
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        _acquire_file_lock(handle, cancel_requested)
        try:
            _raise_if_lock_cancelled(cancel_requested)
            yield
        finally:
            _release_file_lock(handle)


@contextmanager
def _tracked_lock_depth(repository_key: Path) -> Iterator[None]:
    depths = _thread_lock_depths()
    depths[repository_key] = depths.get(repository_key, 0) + 1
    try:
        yield
    finally:
        remaining = depths[repository_key] - 1
        if remaining == 0:
            del depths[repository_key]
        else:
            depths[repository_key] = remaining


def _acquire_repository_lock(
    repository_lock: RLock,
    cancel_requested: Callable[[], bool] | None,
) -> None:
    if cancel_requested is None:
        repository_lock.acquire()
        return
    _raise_if_lock_cancelled(cancel_requested)
    while not repository_lock.acquire(timeout=_LOCK_CANCELLATION_POLL_SECONDS):
        _raise_if_lock_cancelled(cancel_requested)


def _acquire_file_lock(
    handle: BinaryIO,
    cancel_requested: Callable[[], bool] | None,
) -> None:
    file_lock_api = _file_lock_api()
    if cancel_requested is None:
        file_lock_api.flock(handle.fileno(), file_lock_api.LOCK_EX)
        return

    _raise_if_lock_cancelled(cancel_requested)
    while not _try_acquire_file_lock(file_lock_api, handle):
        sleep(_LOCK_CANCELLATION_POLL_SECONDS)
        _raise_if_lock_cancelled(cancel_requested)


def _try_acquire_file_lock(
    file_lock_api: _FileLockApi,
    handle: BinaryIO,
) -> bool:
    try:
        file_lock_api.flock(
            handle.fileno(),
            file_lock_api.LOCK_EX | file_lock_api.LOCK_NB,
        )
    except BlockingIOError:
        return False
    return True


def _release_file_lock(handle: BinaryIO) -> None:
    file_lock_api = _file_lock_api()
    file_lock_api.flock(handle.fileno(), file_lock_api.LOCK_UN)


def _raise_if_lock_cancelled(
    cancel_requested: Callable[[], bool] | None,
) -> None:
    if cancel_requested is not None and cancel_requested():
        raise RuntimeError("Workspace Git metadata lock acquisition was cancelled.")


def _file_lock_api() -> _FileLockApi:
    if fcntl is None:
        raise RuntimeError(
            "Workspace Git metadata locking requires POSIX fcntl. Use WSL or a "
            "POSIX environment for workspace-enabled execution."
        )
    return fcntl


def _repository_lock(repository_key: Path) -> RLock:
    with _LOCKS_GUARD:
        lock = _REPOSITORY_LOCKS.get(repository_key)
        if lock is None:
            lock = RLock()
            _REPOSITORY_LOCKS[repository_key] = lock
        return lock


def _thread_lock_depths() -> dict[Path, int]:
    depths = getattr(_THREAD_LOCK_DEPTHS, "depths", None)
    if depths is None:
        depths = {}
        _THREAD_LOCK_DEPTHS.depths = depths
    return depths


def _lock_depth(repository_key: Path) -> int:
    return _thread_lock_depths().get(repository_key, 0)
