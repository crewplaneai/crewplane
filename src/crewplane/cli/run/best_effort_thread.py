from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Thread


@dataclass(frozen=True)
class BestEffortThreadResult:
    start_error: RuntimeError | None = None
    timed_out: bool = False
    operation_error: Exception | None = None


def run_best_effort_thread(
    target: Callable[[], None],
    name: str,
    timeout_seconds: float,
) -> BestEffortThreadResult:
    operation_errors: list[Exception] = []

    def guarded_target() -> None:
        try:
            target()
        except Exception as exc:
            operation_errors.append(exc)

    thread = Thread(target=guarded_target, name=name, daemon=True)
    try:
        thread.start()
    except RuntimeError as exc:
        return BestEffortThreadResult(start_error=exc)
    thread.join(timeout=timeout_seconds)
    if thread.is_alive():
        return BestEffortThreadResult(timed_out=True)
    if operation_errors:
        return BestEffortThreadResult(operation_error=operation_errors[0])
    return BestEffortThreadResult()
