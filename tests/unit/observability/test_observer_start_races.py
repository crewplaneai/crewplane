from __future__ import annotations

from collections.abc import Callable

import pytest

from crewplane.architecture.contracts import (
    ObserverCapabilities,
    RunContext,
    RunResult,
    WorkflowTopology,
)
from crewplane.observability.observer_lifecycle import start_observer_with_timeout
from tests.integration.observability.runtime.observability_runtime_helpers import (
    RecordingObserver,
)


class LateStartThread:
    def __init__(self, target: Callable[[], None], name: str, daemon: bool) -> None:
        assert name.startswith("crewplane-observer-start-")
        assert daemon
        self.target = target
        self.alive_checks = 0

    def start(self) -> None:
        pass

    def join(self, timeout: float | None = None) -> None:
        assert timeout == 0.1

    def is_alive(self) -> bool:
        self.alive_checks += 1
        if self.alive_checks == 1:
            return True
        self.target()
        return False


@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize("fails", [False, True])
def test_observer_finishing_at_timeout_is_disabled_and_cleaned_once(
    monkeypatch: pytest.MonkeyPatch, required: bool, fails: bool
) -> None:
    observer = RecordingObserver()
    observer.capabilities = ObserverCapabilities(required=required)
    warnings: list[str] = []
    cleanup_count = 0

    def start(context: RunContext) -> None:
        assert context.run_id == "run"
        if fails:
            raise OSError("late start failure")
        observer.started = True

    def stop(result: RunResult) -> None:
        nonlocal cleanup_count
        assert result == RunResult("failed")
        cleanup_count += 1

    monkeypatch.setattr(observer, "start", start)
    monkeypatch.setattr(observer, "stop", stop)
    context = RunContext(WorkflowTopology("flow", ()), "run", 0)
    if required:
        with pytest.raises(TimeoutError, match="observer start timed out"):
            start_observer_with_timeout(
                observer, context, warnings.append, LateStartThread, 0.1
            )
    else:
        assert not start_observer_with_timeout(
            observer, context, warnings.append, LateStartThread, 0.1
        )

    assert cleanup_count == (0 if fails else 1)
    assert observer.started is not fails
    assert warnings[-1] == "observability observer start timed out: RecordingObserver"
    assert any("start failed after timeout" in warning for warning in warnings) is fails


def test_late_start_cleanup_failure_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = RecordingObserver()
    warnings: list[str] = []

    def stop(result: RunResult) -> None:
        assert result == RunResult("failed")
        raise OSError("cleanup failed")

    monkeypatch.setattr(observer, "stop", stop)
    context = RunContext(WorkflowTopology("flow", ()), "run", 0)

    assert not start_observer_with_timeout(
        observer, context, warnings.append, LateStartThread, 0.1
    )
    assert warnings == [
        "observability observer cleanup after start timeout failed: RecordingObserver: cleanup failed",
        "observability observer start timed out: RecordingObserver",
    ]
