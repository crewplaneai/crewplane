from __future__ import annotations

from dataclasses import dataclass
from time import monotonic


def format_elapsed_seconds(seconds: float) -> str:
    normalized = max(0.0, seconds)
    if normalized < 60:
        return f"{normalized:.1f}s"

    minutes, sec = divmod(int(normalized), 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"

    hours, minute = divmod(minutes, 60)
    return f"{hours}h{minute:02d}m{sec:02d}s"


@dataclass
class ElapsedTimer:
    _started_at: float | None = None
    _elapsed_seconds: float | None = None

    def __enter__(self) -> ElapsedTimer:
        self._started_at = monotonic()
        self._elapsed_seconds = None
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        self.stop()

    def stop(self) -> float:
        if self._elapsed_seconds is not None:
            return self._elapsed_seconds
        if self._started_at is None:
            raise RuntimeError("ElapsedTimer has not been started.")
        self._elapsed_seconds = monotonic() - self._started_at
        return self._elapsed_seconds

    @property
    def elapsed_seconds(self) -> float:
        if self._elapsed_seconds is not None:
            return self._elapsed_seconds
        if self._started_at is None:
            raise RuntimeError("ElapsedTimer has not been started.")
        return monotonic() - self._started_at

    @property
    def elapsed_milliseconds(self) -> int:
        return int(self.elapsed_seconds * 1000)
