from __future__ import annotations

from threading import Event, Thread
from types import SimpleNamespace
from typing import cast

import pytest

from crewplane.architecture.contracts import (
    DashboardSnapshot,
    ExecutionEvent,
    ObserverCapabilities,
    RunResult,
    RuntimeObserver,
    WorkflowTopology,
)
from crewplane.observability.events import EventType, runtime_log_event, workflow_event
from crewplane.observability.events.payloads import RuntimeLogEventPayload
from crewplane.observability.observer import validate_observer_contract
from crewplane.observability.runtime import ObservabilityHub
from tests.integration.observability.runtime.observability_runtime_helpers import (
    RecordingObserver,
    RequiredStopFailObserver,
)


@pytest.mark.parametrize(
    ("attribute", "value", "message"),
    [
        ("capabilities", {}, "capabilities must be an ObserverCapabilities"),
        ("stop_requested", "false", "stop_requested must be bool"),
        ("start", None, "lifecycle contract is incomplete"),
        ("on_snapshot", 1, "lifecycle contract is incomplete"),
        ("stop", False, "lifecycle contract is incomplete"),
    ],
)
def test_observer_contract_rejects_invalid_extension_attributes(
    attribute: str, value: object, message: str
) -> None:
    observer = RecordingObserver()
    invalid = SimpleNamespace(
        capabilities=observer.capabilities,
        stop_requested=observer.stop_requested,
        start=observer.start,
        on_snapshot=observer.on_snapshot,
        stop=observer.stop,
    )
    setattr(invalid, attribute, value)

    with pytest.raises(TypeError, match=message):
        validate_observer_contract(cast(RuntimeObserver, invalid))


def test_required_shutdown_failure_preserves_primary_run_error() -> None:
    observer = RequiredStopFailObserver()
    original = RuntimeError("workflow failed")

    with (
        pytest.raises(RuntimeError, match="workflow failed") as caught,
        ObservabilityHub(WorkflowTopology("flow", ()), "run", [observer], 0),
    ):
        raise original

    assert caught.value is original
    assert original.__notes__ == [
        "observability observer shutdown failed: required stop failure"
    ]


def test_duplicate_terminal_event_is_delivered_once() -> None:
    observer = RecordingObserver()
    observer.capabilities = ObserverCapabilities(synchronous_snapshot_delivery=True)
    terminal = workflow_event(EventType.WORKFLOW_FINISHED, "flow", "run")

    with ObservabilityHub(WorkflowTopology("flow", ()), "run", [observer], 0) as hub:
        hub.emit(terminal)
        hub.emit(terminal)

    assert observer.event_types == [None, EventType.WORKFLOW_FINISHED]
    assert observer.stopped


class BlockingDeliveryObserver(RecordingObserver):
    def __init__(self) -> None:
        super().__init__()
        self.entered = Event()
        self.release = Event()
        self.completed = Event()
        self.messages: list[str] = []
        self.result: RunResult | None = None

    def on_snapshot(
        self, event: ExecutionEvent | None, snapshot: DashboardSnapshot
    ) -> None:
        assert snapshot.state.run_id == "run"
        if event is None:
            self.entered.set()
            assert self.release.wait(5), "test did not release observer"
        else:
            assert isinstance(event.payload, RuntimeLogEventPayload)
            self.messages.append(event.payload.message)
            if event.payload.message == "1025":
                self.completed.set()

    def stop(self, result: RunResult) -> None:
        self.result = result


@pytest.mark.parametrize("warning_failure", ["none", "sink", "thread"])
def test_queue_overflow_keeps_recent_events_and_warns_once(
    monkeypatch: pytest.MonkeyPatch, warning_failure: str
) -> None:
    observer = BlockingDeliveryObserver()
    warnings: list[str] = []
    warning_attempted = Event()

    def warn(message: str) -> None:
        warnings.append(message)
        warning_attempted.set()
        if warning_failure == "sink":
            raise RuntimeError("warning sink failed")

    original_start = Thread.start

    def start(thread: Thread) -> None:
        if (
            warning_failure == "thread"
            and thread.name == "crewplane-observability-warning"
        ):
            warning_attempted.set()
            raise RuntimeError("cannot start warning thread")
        original_start(thread)

    with monkeypatch.context() as patch:
        patch.setattr(Thread, "start", start)
        with ObservabilityHub(
            WorkflowTopology("flow", ()), "run", [observer], 0, warn
        ) as hub:
            try:
                assert observer.entered.wait(2)
                for index in range(1026):
                    hub.emit(
                        runtime_log_event(
                            "flow",
                            "run",
                            level="info",
                            operation="test",
                            message=str(index),
                        )
                    )
                assert warning_attempted.wait(2)
            finally:
                observer.release.set()
            assert observer.completed.wait(5)

    assert observer.messages == [str(index) for index in range(2, 1026)]
    assert observer.result == RunResult("succeeded")
    assert len(warnings) == (0 if warning_failure == "thread" else 1)
    if warnings:
        assert "queue is full; dropping stale snapshots" in warnings[0]
