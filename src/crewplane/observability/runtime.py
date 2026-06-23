"""Threaded observability fanout for asyncio-driven workflow runs.

Execution calls emit events from the main runtime path. The hub keeps state
updates locked, delivers slow observer snapshots on a bounded worker queue, and
uses short shutdown timeouts so live UI or summary observers cannot stall the
workflow process indefinitely.
"""

from __future__ import annotations

import copy
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from threading import Event, Lock, Thread
from time import monotonic

from crewplane.observability.events import (
    ExecutionEvent,
    apply_event,
    build_initial_state,
)
from crewplane.observability.layout import compute_topology_layout
from crewplane.observability.observer import Observer
from crewplane.observability.observer_lifecycle import (
    start_observer_with_timeout,
    stop_observers_with_timeout,
)
from crewplane.observability.types import (
    DashboardSnapshot,
    RunContext,
    RunResult,
    WorkflowTopology,
)

WarningSink = Callable[[str], None]

OBSERVER_DELIVERY_JOIN_TIMEOUT_SECONDS = 1.0
OBSERVER_START_TIMEOUT_SECONDS = 5.0
OBSERVER_STOP_TIMEOUT_SECONDS = 1.0
_MAX_OBSERVER_DELIVERY_QUEUE_SIZE = 1024


@dataclass(frozen=True)
class _SnapshotDelivery:
    event: ExecutionEvent | None
    snapshot: DashboardSnapshot
    observers: tuple[Observer, ...]


class ObservabilityHub:
    """Fan out execution snapshots to live and persistent observers."""

    def __init__(
        self,
        workflow_topology: WorkflowTopology,
        run_id: str,
        observers: list[Observer],
        refresh_per_second: int = 4,
        warning_sink: WarningSink | None = None,
    ) -> None:
        self._state = build_initial_state(workflow_topology, run_id)
        self._layout = compute_topology_layout(workflow_topology)
        self._context = RunContext(
            workflow_topology=workflow_topology,
            run_id=run_id,
            refresh_per_second=refresh_per_second,
        )
        self._warning_sink = warning_sink
        self._observers = list(observers)
        self._active_observers: list[Observer] = []
        self._started_observers: list[Observer] = []
        self._refresh_per_second = max(0, refresh_per_second)
        self._refresh_interval = (
            1.0 / self._refresh_per_second if self._refresh_per_second else 0.0
        )
        self._lock = Lock()
        self._delivery_lock = Lock()
        self._stop_event = Event()
        self._delivery_available = Event()
        self._delivery_stop_event = Event()
        self._ticker: Thread | None = None
        self._delivery_worker: Thread | None = None
        self._pending_deliveries: deque[_SnapshotDelivery] = deque()
        self._delivery_queue_warning_emitted = False
        self._terminal_result: RunResult | None = None

    def __enter__(self) -> ObservabilityHub:
        active: list[Observer] = []
        for observer in self._observers:
            if start_observer_with_timeout(
                observer=observer,
                context=self._context,
                warn=self._warn,
                thread_factory=Thread,
                timeout_seconds=OBSERVER_START_TIMEOUT_SECONDS,
            ):
                active.append(observer)

        with self._lock:
            self._active_observers = active
            self._started_observers = list(active)

        self._start_delivery_worker()
        snapshot, observers = self._prepare_snapshot(None)
        self._enqueue_snapshot(None, snapshot, observers, coalesce=True)

        self._start_ticker()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        self._stop_event.set()
        if self._ticker is not None:
            self._ticker.join(timeout=1.0)
            self._ticker = None
        self._stop_delivery_worker()
        result = self._terminal_result or RunResult(
            status="failed" if exc is not None else "succeeded"
        )
        with self._lock:
            started_observers = list(reversed(self._started_observers))
            self._active_observers = []
        try:
            stop_observers_with_timeout(
                observers=started_observers,
                result=result,
                warn=self._warn,
                thread_factory=Thread,
                timeout_seconds=OBSERVER_STOP_TIMEOUT_SECONDS,
            )
        except Exception as stop_exc:
            if exc is not None:
                exc.add_note(f"observability observer shutdown failed: {stop_exc}")
                return
            raise

    def emit(self, event: ExecutionEvent) -> None:
        snapshot, observers = self._prepare_snapshot(event)
        self._enqueue_snapshot(event, snapshot, observers, coalesce=False)

    @property
    def active_observer_count(self) -> int:
        with self._lock:
            return len(self._active_observers)

    def observer_is_active(self, observer: Observer) -> bool:
        with self._lock:
            return observer in self._active_observers

    @property
    def stop_requested(self) -> bool:
        with self._lock:
            observers = list(self._active_observers)
        return any(
            bool(getattr(observer, "stop_requested", False)) for observer in observers
        )

    def set_terminal_result(self, result: RunResult) -> None:
        with self._lock:
            self._terminal_result = result

    def _tick_loop(self) -> None:
        while not self._stop_event.wait(self._refresh_interval):
            snapshot, observers = self._prepare_snapshot(None)
            self._enqueue_snapshot(None, snapshot, observers, coalesce=True)

    def _start_ticker(self) -> None:
        if self._refresh_interval <= 0:
            return
        ticker = Thread(
            target=self._tick_loop,
            name="crewplane-observability-ticker",
            daemon=True,
        )
        try:
            ticker.start()
        except RuntimeError as exc:
            self._ticker = None
            self._warn(f"observability ticker failed to start: {exc}")
            return
        self._ticker = ticker

    def _prepare_snapshot(
        self,
        event: ExecutionEvent | None,
    ) -> tuple[DashboardSnapshot, list[Observer]]:
        with self._lock:
            if event is not None:
                apply_event(self._state, event)
            snapshot = DashboardSnapshot(
                state=copy.deepcopy(self._state),
                layout=self._layout,
                now=monotonic(),
            )
            observers = list(self._active_observers)
        return snapshot, observers

    def _publish_snapshot(
        self,
        event: ExecutionEvent | None,
        snapshot: DashboardSnapshot,
        observers: Sequence[Observer],
    ) -> None:
        failed: list[Observer] = []
        for observer in observers:
            if not self.observer_is_active(observer):
                continue
            try:
                observer.on_snapshot(event, snapshot)
            except Exception as exc:
                self._warn(f"observability observer disabled after error: {exc}")
                failed.append(observer)

        if not failed:
            return

        with self._lock:
            self._active_observers = [
                observer
                for observer in self._active_observers
                if observer not in failed
            ]

    def _publish_synchronous_snapshot(
        self,
        event: ExecutionEvent | None,
        snapshot: DashboardSnapshot,
        observers: Sequence[Observer],
    ) -> tuple[Observer, ...]:
        remaining: list[Observer] = []
        failed: list[Observer] = []
        for observer in observers:
            if not self.observer_is_active(observer):
                continue
            if not bool(getattr(observer, "synchronous_snapshot_delivery", False)):
                remaining.append(observer)
                continue
            try:
                observer.on_snapshot(event, snapshot)
            except Exception as exc:
                self._warn(f"observability observer disabled after error: {exc}")
                failed.append(observer)
                if bool(getattr(observer, "required", False)):
                    with self._lock:
                        self._active_observers = [
                            existing
                            for existing in self._active_observers
                            if existing not in failed
                        ]
                    raise

        if failed:
            with self._lock:
                self._active_observers = [
                    observer
                    for observer in self._active_observers
                    if observer not in failed
                ]

        return tuple(remaining)

    def _start_delivery_worker(self) -> None:
        self._delivery_stop_event.clear()
        self._delivery_available.clear()
        delivery_worker = Thread(
            target=self._delivery_loop,
            name="crewplane-observability-delivery",
            daemon=True,
        )
        try:
            delivery_worker.start()
        except RuntimeError as exc:
            self._delivery_worker = None
            self._warn(f"observability observer delivery worker failed to start: {exc}")
            return
        self._delivery_worker = delivery_worker

    def _stop_delivery_worker(self) -> None:
        self._delivery_stop_event.set()
        self._delivery_available.set()
        if self._delivery_worker is None:
            return
        self._delivery_worker.join(timeout=OBSERVER_DELIVERY_JOIN_TIMEOUT_SECONDS)
        if self._delivery_worker.is_alive():
            with self._delivery_lock:
                self._pending_deliveries.clear()
            self._warn(
                "observability observer delivery did not stop promptly; continuing"
            )

    def _enqueue_snapshot(
        self,
        event: ExecutionEvent | None,
        snapshot: DashboardSnapshot,
        observers: Iterable[Observer],
        coalesce: bool,
    ) -> None:
        remaining_observers = self._publish_synchronous_snapshot(
            event,
            snapshot,
            tuple(observers),
        )
        if not remaining_observers:
            return
        if self._delivery_worker is None:
            self._publish_snapshot(event, snapshot, remaining_observers)
            return
        delivery = _SnapshotDelivery(
            event=event,
            snapshot=snapshot,
            observers=remaining_observers,
        )
        should_warn = False
        with self._delivery_lock:
            if coalesce and any(
                pending.event is None for pending in self._pending_deliveries
            ):
                return
            if len(self._pending_deliveries) >= _MAX_OBSERVER_DELIVERY_QUEUE_SIZE:
                self._pending_deliveries.popleft()
                should_warn = not self._delivery_queue_warning_emitted
                self._delivery_queue_warning_emitted = True
            self._pending_deliveries.append(delivery)
            self._delivery_available.set()
        if should_warn:
            self._warn_async(
                "observability observer delivery queue is full; dropping stale snapshots"
            )

    def _delivery_loop(self) -> None:
        while True:
            self._delivery_available.wait()
            while True:
                delivery = self._take_pending_delivery()
                if delivery is None:
                    if self._delivery_stop_event.is_set():
                        return
                    break
                self._publish_snapshot(
                    delivery.event, delivery.snapshot, delivery.observers
                )

    def _take_pending_delivery(self) -> _SnapshotDelivery | None:
        with self._delivery_lock:
            if not self._pending_deliveries:
                self._delivery_available.clear()
                return None
            delivery = self._pending_deliveries.popleft()
            if not self._pending_deliveries:
                self._delivery_available.clear()
            return delivery

    def _warn(self, message: str) -> None:
        if self._warning_sink is None:
            return
        try:
            self._warning_sink(message)
        except Exception:
            return

    def _warn_async(self, message: str) -> None:
        warning_thread = Thread(
            target=self._warn,
            args=(message,),
            name="crewplane-observability-warning",
            daemon=True,
        )
        try:
            warning_thread.start()
        except RuntimeError:
            return
