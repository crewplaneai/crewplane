from __future__ import annotations

from pathlib import Path
from threading import Lock

_LOCK = Lock()
_UNRESOLVED: set[Path] = set()


def fence_workspace_mutator(state_path: Path) -> None:
    with _LOCK:
        _UNRESOLVED.add(state_path.resolve(strict=False))


def release_workspace_mutator(state_path: Path) -> None:
    with _LOCK:
        _UNRESOLVED.discard(state_path.resolve(strict=False))


def workspace_mutator_is_fenced(state_path: Path) -> bool:
    with _LOCK:
        return state_path.resolve(strict=False) in _UNRESOLVED
