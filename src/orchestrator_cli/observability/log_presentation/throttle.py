from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from orchestrator_cli.observability.events.types import InvocationStatus

from .limits import DEFAULT_LIMITS, LogPresentationLimits


@dataclass
class _ThrottleEntry:
    checked_at: float
    size_bytes: int


class IncompleteJsonObjectThrottle:
    def __init__(self) -> None:
        self._entries: dict[str, _ThrottleEntry] = {}

    def should_parse(
        self,
        path: Path,
        status: InvocationStatus,
        size_bytes: int,
        limits: LogPresentationLimits = DEFAULT_LIMITS,
        now: float | None = None,
    ) -> bool:
        if status in {"succeeded", "failed"}:
            self.clear_path(path)
            return True
        current_time = monotonic() if now is None else now
        key = _path_key(path)
        entry = self._entries.get(key)
        if entry is None:
            return True
        if size_bytes < entry.size_bytes:
            self.clear_path(path)
            return True
        return (
            current_time - entry.checked_at
            >= limits.incomplete_json_object_min_parse_interval_seconds
        )

    def mark_incomplete(
        self,
        path: Path,
        size_bytes: int,
        now: float | None = None,
    ) -> None:
        self._entries[_path_key(path)] = _ThrottleEntry(
            checked_at=monotonic() if now is None else now,
            size_bytes=size_bytes,
        )

    def clear_path(self, path: Path) -> None:
        self._entries.pop(_path_key(path), None)

    def clear_all(self) -> None:
        self._entries.clear()


JSON_OBJECT_THROTTLE = IncompleteJsonObjectThrottle()


def _path_key(path: Path) -> str:
    return str(path.expanduser().resolve(strict=False))
