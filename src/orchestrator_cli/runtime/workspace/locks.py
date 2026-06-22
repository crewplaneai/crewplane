from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock, RLock, local
from typing import BinaryIO

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on native Windows.
    fcntl = None  # type: ignore[assignment]


_LOCKS_GUARD = Lock()
_REPOSITORY_LOCKS: dict[Path, RLock] = {}
_THREAD_LOCK_DEPTHS = local()


@contextmanager
def git_metadata_lock(common_git_dir: Path) -> Iterator[None]:
    if fcntl is None:
        raise RuntimeError(
            "Workspace Git metadata locking requires POSIX fcntl. Use WSL or a "
            "POSIX environment for workspace-enabled execution."
        )
    repository_lock = _repository_lock(common_git_dir)
    repository_key = common_git_dir.resolve(strict=False)
    lock_dir = common_git_dir / "orchestrator-cli"
    handle: BinaryIO | None = None
    with repository_lock:
        outermost = _lock_depth(repository_key) == 0
        if outermost:
            lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            lock_path = lock_dir / "workspace.lock"
            handle = lock_path.open("a+b")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except Exception:
                handle.close()
                raise
        _increment_lock_depth(repository_key)
        try:
            yield
        finally:
            _decrement_lock_depth(repository_key)
            if outermost:
                if handle is None:
                    raise RuntimeError("Workspace Git metadata lock handle missing.")
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()


def _repository_lock(common_git_dir: Path) -> RLock:
    key = common_git_dir.resolve(strict=False)
    with _LOCKS_GUARD:
        lock = _REPOSITORY_LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _REPOSITORY_LOCKS[key] = lock
        return lock


def _thread_lock_depths() -> dict[Path, int]:
    depths = getattr(_THREAD_LOCK_DEPTHS, "depths", None)
    if depths is None:
        depths = {}
        _THREAD_LOCK_DEPTHS.depths = depths
    return depths


def _lock_depth(repository_key: Path) -> int:
    return _thread_lock_depths().get(repository_key, 0)


def _increment_lock_depth(repository_key: Path) -> None:
    depths = _thread_lock_depths()
    depths[repository_key] = depths.get(repository_key, 0) + 1


def _decrement_lock_depth(repository_key: Path) -> None:
    depths = _thread_lock_depths()
    remaining = depths.get(repository_key, 0) - 1
    if remaining <= 0:
        depths.pop(repository_key, None)
    else:
        depths[repository_key] = remaining
