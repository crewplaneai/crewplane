from __future__ import annotations

from collections.abc import Callable, Iterable
from threading import Event, Lock
from typing import Protocol

from orchestrator_cli.observability.observer import Observer
from orchestrator_cli.observability.types import RunContext, RunResult

WarningSink = Callable[[str], None]


class LifecycleThread(Protocol):
    def start(self) -> None: ...

    def join(self, timeout: float | None = None) -> None: ...

    def is_alive(self) -> bool: ...


class LifecycleThreadFactory(Protocol):
    def __call__(
        self,
        target: Callable[[], None],
        name: str,
        daemon: bool,
    ) -> LifecycleThread: ...


def start_observer_with_timeout(
    observer: Observer,
    context: RunContext,
    warn: WarningSink,
    thread_factory: LifecycleThreadFactory,
    timeout_seconds: float,
) -> bool:
    failure: list[Exception] = []
    timed_out = _new_event()
    cleanup_guard = _CleanupGuard()

    def cleanup_after_timeout() -> None:
        if not cleanup_guard.mark_done():
            return
        stop_timed_out_observer(observer, warn)

    def start_observer() -> None:
        try:
            observer.start(context)
        except Exception as exc:
            failure.append(exc)
            if timed_out.is_set():
                warn(
                    "observability observer start failed after timeout: "
                    f"{observer.__class__.__name__}: {exc}"
                )
            return
        if timed_out.is_set():
            cleanup_after_timeout()

    start_thread = thread_factory(
        target=start_observer,
        name=f"orchestrator-observer-start-{observer.__class__.__name__}",
        daemon=True,
    )
    try:
        start_thread.start()
    except RuntimeError as exc:
        warn(
            "observability observer start thread failed: "
            f"{observer.__class__.__name__}: {exc}"
        )
        if bool(getattr(observer, "required", False)):
            raise
        return False
    start_thread.join(timeout=timeout_seconds)
    if start_thread.is_alive():
        timed_out.set()
        if not start_thread.is_alive() and not failure:
            cleanup_after_timeout()
        warn(f"observability observer start timed out: {observer.__class__.__name__}")
        if bool(getattr(observer, "required", False)):
            raise TimeoutError(
                f"observability observer start timed out: {observer.__class__.__name__}"
            )
        return False
    if failure:
        warn(f"observability observer start failed: {failure[0]}")
        if bool(getattr(observer, "required", False)):
            raise failure[0]
        return False
    return True


def stop_timed_out_observer(observer: Observer, warn: WarningSink) -> None:
    if not bool(getattr(observer, "cleanup_after_start_timeout", True)):
        return
    try:
        observer.stop(RunResult(status="failed"))
    except Exception as exc:
        warn(
            "observability observer cleanup after start timeout failed: "
            f"{observer.__class__.__name__}: {exc}"
        )


def stop_observers_with_timeout(
    observers: Iterable[Observer],
    result: RunResult,
    warn: WarningSink,
    thread_factory: LifecycleThreadFactory,
    timeout_seconds: float,
) -> None:
    required_failures: list[Exception] = []
    for observer in observers:
        failure: list[Exception] = []

        def stop_observer(
            target_observer: Observer = observer,
            target_failure: list[Exception] = failure,
        ) -> None:
            try:
                target_observer.stop(result)
            except Exception as exc:
                target_failure.append(exc)

        stop_thread = thread_factory(
            target=stop_observer,
            name=f"orchestrator-observer-stop-{observer.__class__.__name__}",
            daemon=True,
        )
        try:
            stop_thread.start()
        except RuntimeError as exc:
            warn(
                "observability observer stop thread failed: "
                f"{observer.__class__.__name__}: {exc}"
            )
            if bool(getattr(observer, "required", False)):
                required_failures.append(exc)
            continue
        stop_thread.join(timeout=timeout_seconds)
        if stop_thread.is_alive():
            message = (
                f"observability observer stop timed out: {observer.__class__.__name__}"
            )
            warn(message)
            if bool(getattr(observer, "required", False)):
                required_failures.append(TimeoutError(message))
            continue
        if failure:
            warn(f"observability observer stop failed: {failure[0]}")
            if bool(getattr(observer, "required", False)):
                required_failures.append(failure[0])

    if required_failures:
        raise required_failures[0]


class _CleanupGuard:
    def __init__(self) -> None:
        self._lock = Lock()
        self._done = False

    def mark_done(self) -> bool:
        with self._lock:
            if self._done:
                return False
            self._done = True
            return True


def _new_event() -> Event:
    return Event()
