from __future__ import annotations

from pathlib import Path

import pytest

from crewplane.architecture.contracts import (
    TERMINAL_WORKFLOW_EVENT_TYPES,
    DashboardSnapshot,
    EventType,
    ObserverCapabilities,
)
from crewplane.artifacts.locks import SameContextLock, acquire_same_context_lock
from crewplane.artifacts.locks.manifest import TerminalRecoveryPhase
from crewplane.artifacts.manager import OutputManager
from crewplane.cli.run.terminalization import (
    TerminalizationCoordinator,
    commit_terminalization_with_retry,
)
from crewplane.cli.run.topology import workflow_topology_from_plan
from crewplane.core.execution_state import RunManifest, TerminalRunStatus
from crewplane.observability import ObservabilityHub
from crewplane.observability.events import ExecutionEvent, read_event_log
from crewplane.observability.persistent import PersistentRunLogger
from tests.helpers.resume import (
    WORKFLOW_IDENTITY,
    WORKFLOW_NAME,
    WORKFLOW_SIGNATURE,
    make_plan,
    make_run_manifest,
)
from tests.helpers.resume_locks import FakeProcessInspector


class _FailOnceTerminalEventLogger(PersistentRunLogger):
    def __init__(self, output: OutputManager) -> None:
        super().__init__(output)
        self.terminal_event_attempts = 0

    def on_snapshot(
        self,
        event: ExecutionEvent | None,
        snapshot: DashboardSnapshot,
    ) -> None:
        if event is not None and event.event_type == EventType.WORKFLOW_FINISHED:
            self.terminal_event_attempts += 1
            if self.terminal_event_attempts == 1:
                raise OSError("terminal event append failed")
        super().on_snapshot(event, snapshot)


class _WriteThenFailOnceTerminalEventLogger(PersistentRunLogger):
    def __init__(self, output: OutputManager) -> None:
        super().__init__(output)
        self.terminal_event_attempts = 0

    def on_snapshot(
        self,
        event: ExecutionEvent | None,
        snapshot: DashboardSnapshot,
    ) -> None:
        super().on_snapshot(event, snapshot)
        if event is None or event.event_type != EventType.WORKFLOW_FINISHED:
            return
        self.terminal_event_attempts += 1
        if self.terminal_event_attempts == 1:
            raise OSError("terminal event delivery failed after append")


class _AlwaysFailTerminalEventLogger(PersistentRunLogger):
    def __init__(self, output: OutputManager) -> None:
        super().__init__(output)
        self.terminal_event_attempts = 0

    def on_snapshot(
        self,
        event: ExecutionEvent | None,
        snapshot: DashboardSnapshot,
    ) -> None:
        if event is not None and event.event_type == EventType.WORKFLOW_FINISHED:
            self.terminal_event_attempts += 1
            raise OSError("terminal event append failed permanently")
        super().on_snapshot(event, snapshot)


class _FailOnceRequiredTerminalObserver:
    capabilities = ObserverCapabilities(
        required=True,
        synchronous_snapshot_delivery=True,
    )

    def __init__(self) -> None:
        self.terminal_event_attempts = 0

    @property
    def stop_requested(self) -> bool:
        return False

    def start(self, context: object) -> None:
        del context

    def on_snapshot(
        self,
        event: ExecutionEvent | None,
        snapshot: DashboardSnapshot,
    ) -> None:
        del snapshot
        if event is None or event.event_type != EventType.WORKFLOW_FINISHED:
            return
        self.terminal_event_attempts += 1
        if self.terminal_event_attempts == 1:
            raise OSError("secondary terminal delivery failed")

    def stop(self, result: object) -> None:
        del result


class _FailingTerminalViewsRecovery:
    def __init__(self, run_lock: SameContextLock) -> None:
        self.run_lock = run_lock

    def record_terminal_recovery(
        self,
        phase: TerminalRecoveryPhase,
        status: TerminalRunStatus,
        reason: str | None,
    ) -> None:
        if phase == "terminal_views_published":
            raise OSError("terminal views phase publication failed")
        self.run_lock.record_terminal_recovery(phase, status, reason)


def _acquire_test_run_lock(tmp_path: Path, output: OutputManager) -> SameContextLock:
    run_lock = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(100, "old"),
    )
    run_lock.update_run(output.run_id, output.run_key_name)
    return run_lock


def test_terminal_event_retry_requires_exact_durable_event(
    tmp_path: Path,
) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path, template_base_dir=tmp_path)
    output.write_run_manifest(
        make_run_manifest(output.run_id, output.run_key_name, status="running")
    )
    logger = _FailOnceTerminalEventLogger(output)
    coordinator = TerminalizationCoordinator(output, "Workflow")
    coordinator.bind_summary_logger(logger)

    with ObservabilityHub(
        workflow_topology=workflow_topology_from_plan(make_plan()),
        run_id=output.run_id,
        observers=[logger],
        refresh_per_second=0,
    ) as hub:
        commit_terminalization_with_retry(coordinator, hub, "succeeded")

    coordinator.acknowledge_observer_shutdown()
    terminal_events = [
        event
        for event in read_event_log(output.get_run_event_log_path())
        if event.event_type == EventType.WORKFLOW_FINISHED
    ]
    assert logger.terminal_event_attempts == 2
    assert len(terminal_events) == 1
    assert coordinator.event_published is True
    assert coordinator.committed is True


def test_terminal_event_retry_does_not_duplicate_completed_append(
    tmp_path: Path,
) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path, template_base_dir=tmp_path)
    output.write_run_manifest(
        make_run_manifest(output.run_id, output.run_key_name, status="running")
    )
    logger = _WriteThenFailOnceTerminalEventLogger(output)
    coordinator = TerminalizationCoordinator(output, "Workflow")
    coordinator.bind_summary_logger(logger)

    with ObservabilityHub(
        workflow_topology=workflow_topology_from_plan(make_plan()),
        run_id=output.run_id,
        observers=[logger],
        refresh_per_second=0,
    ) as hub:
        commit_terminalization_with_retry(coordinator, hub, "succeeded")

    coordinator.acknowledge_observer_shutdown()
    terminal_events = [
        event
        for event in read_event_log(output.get_run_event_log_path())
        if event.event_type == EventType.WORKFLOW_FINISHED
    ]
    assert logger.terminal_event_attempts == 2
    assert len(terminal_events) == 1
    assert coordinator.event_published is True
    assert coordinator.committed is True


def test_permanent_terminal_event_failure_leaves_manifest_running(
    tmp_path: Path,
) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path, template_base_dir=tmp_path)
    output.write_run_manifest(
        make_run_manifest(output.run_id, output.run_key_name, status="running")
    )
    stale = _acquire_test_run_lock(tmp_path, output)
    logger = _AlwaysFailTerminalEventLogger(output)
    coordinator = TerminalizationCoordinator(
        output,
        "Workflow",
        terminal_recovery_recorder=stale,
    )
    coordinator.bind_summary_logger(logger)

    with (
        pytest.raises(
            OSError,
            match="terminal event append failed permanently",
        ),
        ObservabilityHub(
            workflow_topology=workflow_topology_from_plan(make_plan()),
            run_id=output.run_id,
            observers=[logger],
            refresh_per_second=0,
        ) as hub,
    ):
        commit_terminalization_with_retry(coordinator, hub, "succeeded")

    manifest = RunManifest.model_validate_json(
        (output.stages_dir / "manifests" / "run.json").read_text(encoding="utf-8")
    )
    assert logger.terminal_event_attempts == 2
    assert manifest.status == "running"
    assert coordinator.manifest_published is False
    assert coordinator.event_published is False
    assert coordinator.summary_published is False
    assert coordinator.committed is False
    assert coordinator.recovery_phase == "outcome_selected"

    replacement = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(200, "new", live=False),
    )
    try:
        recovered = RunManifest.model_validate_json(
            (output.stages_dir / "manifests" / "run.json").read_text(encoding="utf-8")
        )
        assert recovered.status == "cancelled"
        assert recovered.cancel_reason == "stale_lock_recovered"
        terminal_events = [
            event
            for event in read_event_log(output.get_run_event_log_path())
            if event.event_type in TERMINAL_WORKFLOW_EVENT_TYPES
        ]
        assert terminal_events == []
        assert "- Status: failed" in output.get_run_summary_path().read_text(
            encoding="utf-8"
        )
    finally:
        replacement.release()


@pytest.mark.parametrize(
    ("status", "reason", "event_type"),
    [
        ("succeeded", None, EventType.WORKFLOW_FINISHED),
        ("failed", "execution failed", EventType.WORKFLOW_FAILED),
        ("cancelled", "external_cancellation", EventType.WORKFLOW_CANCELLED),
    ],
)
@pytest.mark.parametrize(
    "summary_edit",
    [
        None,
        ("- Workflow: ", "- Workflow: wrong-"),
        ("- Run ID: ", "- Run ID: wrong-"),
        ("- Status: ", "- Status: {status}\n- Status: "),
        ("- Status: ", "- Status: running\n- Status: "),
        ("# Run Summary\n\n", "# Run Summary\n"),
        ("# Run Summary", "# Summary"),
        ("- Status: ", "- Status:"),
    ],
    ids=[
        "valid",
        "workflow",
        "run-id",
        "duplicate",
        "conflict",
        "blank",
        "heading",
        "status",
    ],
)
def test_stale_recovery_replays_views_when_phase_publication_fails(
    tmp_path: Path,
    status: TerminalRunStatus,
    reason: str | None,
    event_type: EventType,
    summary_edit: tuple[str, str] | None,
) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path, template_base_dir=tmp_path)
    output.write_run_manifest(
        make_run_manifest(output.run_id, output.run_key_name, status="running")
    )
    stale = _acquire_test_run_lock(tmp_path, output)
    logger = PersistentRunLogger(output)
    coordinator = TerminalizationCoordinator(
        output,
        "Workflow",
        terminal_recovery_recorder=_FailingTerminalViewsRecovery(stale),
    )
    coordinator.bind_summary_logger(logger)

    with (
        pytest.raises(OSError, match="terminal views phase publication failed"),
        ObservabilityHub(
            workflow_topology=workflow_topology_from_plan(make_plan()),
            run_id=output.run_id,
            observers=[logger],
            refresh_per_second=0,
        ) as hub,
    ):
        commit_terminalization_with_retry(coordinator, hub, status, reason)

    assert coordinator.recovery_phase == "outcome_selected"
    summary_path = output.get_run_summary_path()
    summary_text = summary_path.read_text(encoding="utf-8")
    assert f"- Status: {status}\n" in summary_text
    if summary_edit is not None:
        old, new = summary_edit
        assert old in summary_text
        summary_path.write_text(
            summary_text.replace(old, new.format(status=status)), encoding="utf-8"
        )
    replacement = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(200, "new", live=False),
    )
    try:
        recovered = RunManifest.model_validate_json(
            (output.stages_dir / "manifests" / "run.json").read_text(encoding="utf-8")
        )
        assert recovered.status == (status if summary_edit is None else "cancelled")
        assert recovered.failure_message == (
            reason if summary_edit is None and status == "failed" else None
        )
        assert recovered.cancel_reason == (
            "stale_lock_recovered"
            if summary_edit is not None
            else reason
            if status == "cancelled"
            else None
        )
        terminal_events = [
            event.event_type
            for event in read_event_log(output.get_run_event_log_path())
            if event.event_type in TERMINAL_WORKFLOW_EVENT_TYPES
        ]
        assert terminal_events == [event_type]
    finally:
        replacement.release()


def test_terminal_event_retry_reaches_all_required_synchronous_observers(
    tmp_path: Path,
) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path, template_base_dir=tmp_path)
    output.write_run_manifest(
        make_run_manifest(output.run_id, output.run_key_name, status="running")
    )
    logger = PersistentRunLogger(output)
    secondary_observer = _FailOnceRequiredTerminalObserver()
    coordinator = TerminalizationCoordinator(output, "Workflow")
    coordinator.bind_summary_logger(logger)

    with ObservabilityHub(
        workflow_topology=workflow_topology_from_plan(make_plan()),
        run_id=output.run_id,
        observers=[logger, secondary_observer],
        refresh_per_second=0,
    ) as hub:
        commit_terminalization_with_retry(coordinator, hub, "succeeded")

    coordinator.acknowledge_observer_shutdown()
    terminal_events = [
        event
        for event in read_event_log(output.get_run_event_log_path())
        if event.event_type == EventType.WORKFLOW_FINISHED
    ]
    assert secondary_observer.terminal_event_attempts == 2
    assert len(terminal_events) == 1
    assert coordinator.event_published is True
    assert coordinator.committed is True
