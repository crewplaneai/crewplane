from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from rich.console import Console

from crewplane.architecture.contracts import (
    TERMINAL_WORKFLOW_EVENT_TYPES,
    DashboardSnapshot,
    EventType,
    ObserverCapabilities,
)
from crewplane.architecture.ports.runtime import RuntimeComponents
from crewplane.artifacts.locks import SameContextLock, acquire_same_context_lock
from crewplane.artifacts.locks.manifest import TerminalRecoveryPhase
from crewplane.artifacts.manager import OutputManager
from crewplane.cli.run import execution as execution_module
from crewplane.cli.run.context import WorkflowRunContext
from crewplane.cli.run.observability import WorkflowWarningRecorder
from crewplane.cli.run.terminalization import (
    TerminalizationCoordinator,
    commit_terminalization_with_retry,
)
from crewplane.cli.run.topology import workflow_topology_from_plan
from crewplane.core.config import Config
from crewplane.core.execution_state import RunManifest, TerminalRunStatus
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.preflight.source import PreflightWorkflowSource
from crewplane.core.workflow.models import WorkflowPlan
from crewplane.observability import ObservabilityHub
from crewplane.observability.events import ExecutionEvent, read_event_log
from crewplane.observability.persistent import PersistentRunLogger
from crewplane.observability.types import RunResult
from crewplane.version import SCHEMA_VERSION
from tests.helpers.resume import (
    WORKFLOW_IDENTITY,
    WORKFLOW_NAME,
    WORKFLOW_SIGNATURE,
    make_plan,
    make_run_manifest,
)
from tests.helpers.resume_locks import FakeProcessInspector


class _RequiredStopFailureObserver:
    capabilities = ObserverCapabilities(required=True)

    @property
    def stop_requested(self) -> bool:
        return False

    def start(self, context: object) -> None:
        del context

    def on_snapshot(self, event: object, snapshot: object) -> None:
        del event, snapshot

    def stop(self, result: object) -> None:
        del result
        raise RuntimeError("required observer stop failed")


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


class _RecordingTerminalRecovery:
    def __init__(self) -> None:
        self.calls: list[tuple[str, TerminalRunStatus, str | None]] = []

    def record_terminal_recovery(
        self,
        phase: str,
        status: TerminalRunStatus,
        reason: str | None,
    ) -> None:
        self.calls.append((phase, status, reason))


class _FailingSelectedOutcomeRecovery:
    def record_terminal_recovery(
        self,
        phase: str,
        status: TerminalRunStatus,
        reason: str | None,
    ) -> None:
        del status, reason
        if phase == "outcome_selected":
            raise OSError("selected outcome publication failed")


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


def test_selected_outcome_is_persisted_before_terminal_views(tmp_path: Path) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path, template_base_dir=tmp_path)
    output.write_run_manifest(
        make_run_manifest(output.run_id, output.run_key_name, status="running")
    )
    coordinator = TerminalizationCoordinator(
        output,
        "Workflow",
        terminal_recovery_recorder=_FailingSelectedOutcomeRecovery(),
    )
    logger = PersistentRunLogger(output)
    coordinator.bind_summary_logger(logger)

    with (
        pytest.raises(OSError, match="selected outcome publication failed"),
        ObservabilityHub(
            workflow_topology=workflow_topology_from_plan(make_plan()),
            run_id=output.run_id,
            observers=[logger],
            refresh_per_second=0,
        ) as hub,
    ):
        coordinator.commit(hub, "succeeded")

    assert coordinator.event_published is False
    assert coordinator.result_published is False
    assert coordinator.summary_published is False


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
def test_manifest_publication_failure_leaves_running_commit_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: TerminalRunStatus,
) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path, template_base_dir=tmp_path)
    output.write_run_manifest(
        make_run_manifest(output.run_id, output.run_key_name, status="running")
    )
    terminal_recovery = _RecordingTerminalRecovery()
    coordinator = TerminalizationCoordinator(
        output,
        "Workflow",
        terminal_recovery_recorder=terminal_recovery,
    )
    logger = PersistentRunLogger(output)
    coordinator.bind_summary_logger(logger)

    def fail_manifest_publication(*args: object, **kwargs: object) -> Path:
        del args, kwargs
        raise OSError("manifest publication failed")

    monkeypatch.setattr(
        output,
        "update_run_manifest_status",
        fail_manifest_publication,
    )

    with ObservabilityHub(
        workflow_topology=workflow_topology_from_plan(make_plan()),
        run_id=output.run_id,
        observers=[logger],
        refresh_per_second=0,
    ) as hub:
        coordinator.commit(hub, status)
        coordinator.commit(hub, status)

    expected_reason = {
        "succeeded": None,
        "failed": "Workflow execution failed.",
        "cancelled": "Workflow execution was cancelled.",
    }[status]
    assert terminal_recovery.calls == [
        ("outcome_selected", status, expected_reason),
        ("terminal_views_published", status, expected_reason),
    ]
    with pytest.raises(OSError, match="manifest publication failed"):
        coordinator.acknowledge_observer_shutdown()

    assert terminal_recovery.calls == [
        ("outcome_selected", status, expected_reason),
        ("terminal_views_published", status, expected_reason),
        ("observer_shutdown_complete", status, expected_reason),
    ]
    assert coordinator.recovery_phase == "observer_shutdown_complete"
    assert coordinator.manifest_published is False
    assert coordinator.event_published is True
    assert coordinator.result_published is True
    assert coordinator.summary_published is True
    assert coordinator.committed is False
    manifest = RunManifest.model_validate_json(
        (output.stages_dir / "manifests" / "run.json").read_text(encoding="utf-8")
    )
    assert manifest.status == "running"


def test_manifest_publication_retries_once_after_observer_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path, template_base_dir=tmp_path)
    output.write_run_manifest(
        make_run_manifest(output.run_id, output.run_key_name, status="running")
    )
    logger = PersistentRunLogger(output)
    coordinator = TerminalizationCoordinator(output, "Workflow")
    coordinator.bind_summary_logger(logger)
    original_update = output.update_run_manifest_status
    manifest_attempts = 0

    def fail_once(
        status: TerminalRunStatus,
        completed_at: str,
        failure_message: str | None = None,
        cancel_reason: str | None = None,
    ) -> Path:
        nonlocal manifest_attempts
        manifest_attempts += 1
        if manifest_attempts == 1:
            raise OSError("transient manifest publication failure")
        return original_update(
            status=status,
            completed_at=completed_at,
            failure_message=failure_message,
            cancel_reason=cancel_reason,
        )

    monkeypatch.setattr(output, "update_run_manifest_status", fail_once)

    with ObservabilityHub(
        workflow_topology=workflow_topology_from_plan(make_plan()),
        run_id=output.run_id,
        observers=[logger],
        refresh_per_second=0,
    ) as hub:
        coordinator.commit(hub, "succeeded")

    coordinator.acknowledge_observer_shutdown()

    manifest = RunManifest.model_validate_json(
        (output.stages_dir / "manifests" / "run.json").read_text(encoding="utf-8")
    )
    assert manifest_attempts == 2
    assert manifest.status == "succeeded"
    assert coordinator.manifest_published is True
    assert coordinator.committed is True


def test_post_manifest_failure_is_retryable_without_duplicate_terminal_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path, template_base_dir=tmp_path)
    output.write_run_manifest(
        make_run_manifest(output.run_id, output.run_key_name, status="running")
    )
    logger = PersistentRunLogger(output)
    coordinator = TerminalizationCoordinator(output, "Workflow")
    coordinator.bind_summary_logger(logger)
    original_refresh = logger.refresh_summary
    refresh_calls = 0

    def fail_once(result: RunResult) -> object:
        nonlocal refresh_calls
        refresh_calls += 1
        if refresh_calls == 1:
            raise OSError("summary publication failed")
        return original_refresh(result)

    monkeypatch.setattr(logger, "refresh_summary", fail_once)

    with ObservabilityHub(
        workflow_topology=workflow_topology_from_plan(make_plan()),
        run_id=output.run_id,
        observers=[logger],
        refresh_per_second=0,
    ) as hub:
        with pytest.raises(OSError, match="summary publication failed"):
            coordinator.commit(hub, "succeeded")

        assert coordinator.manifest_published is False
        assert coordinator.event_published is True
        assert coordinator.result_published is True
        assert coordinator.summary_published is False
        assert coordinator.committed is False
        coordinator.commit(hub, "succeeded")

    assert coordinator.committed is False
    coordinator.acknowledge_observer_shutdown()
    assert coordinator.manifest_published is True
    assert coordinator.committed is True
    terminal_events = [
        event
        for event in read_event_log(output.get_run_event_log_path())
        if event.event_type == EventType.WORKFLOW_FINISHED
    ]
    assert len(terminal_events) == 1


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


def test_branch_export_failure_preserves_failed_run_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = make_plan()
    console = Console(file=io.StringIO(), force_terminal=False, color_system=None)
    output = OutputManager("Workflow", base_dir=tmp_path, template_base_dir=tmp_path)
    context = WorkflowRunContext(
        config=Config(version=SCHEMA_VERSION, agents={}),
        source=PreflightWorkflowSource.from_workflow(
            WorkflowPlan(name=plan.workflow_name, nodes=[]),
        ),
        console=console,
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
    )
    components = RuntimeComponents(
        artifact_store=output,
        base_invoker=object(),
        observers=(),
        suppress_progress_output=False,
    )
    warning_recorder = WorkflowWarningRecorder(
        workflow=context.workflow,
        console=console,
    )
    export_error = RuntimeError("branch export failed")
    output.write_run_manifest(
        make_run_manifest(output.run_id, output.run_key_name, status="running")
    )
    terminalization = TerminalizationCoordinator(
        output=output,
        workflow_name=plan.workflow_name,
    )

    async def complete_execution(
        *args: object,  # noqa: ARG001 - Required by execution test double.
        **kwargs: object,  # noqa: ARG001 - Required by execution test double.
    ) -> None:
        return None

    def fail_branch_export(
        plan_arg: object,  # noqa: ARG001 - Required by branch-export test double.
        output_arg: object,  # noqa: ARG001 - Required by branch-export test double.
        resumed_node_ids: tuple[str, ...],  # noqa: ARG001 - Test double contract.
    ) -> tuple[Path, ...]:
        raise export_error

    monkeypatch.setattr(
        execution_module,
        "fulfill_branch_exports",
        fail_branch_export,
    )

    with pytest.raises(RuntimeError, match="branch export failed") as raised:
        asyncio.run(
            execution_module.run_and_finalize_workflow(
                context=context,
                output=output,
                components=components,
                plan=plan,
                secret_context=SecretContext(),
                execute_workflow_impl=complete_execution,
                warning_recorder=warning_recorder,
                observability_hub_cls=None,
                workflow_identity=".crewplane/workflows/workflow.task.md",
                terminalization=terminalization,
            )
        )

    assert raised.value is export_error
    assert terminalization.committed is True
    manifest = RunManifest.model_validate_json(
        (output.stages_dir / "manifests" / "run.json").read_text(encoding="utf-8")
    )
    assert manifest.status == "failed"
    assert manifest.failure_message == "branch export failed"
    terminal_events = [
        event
        for event in read_event_log(output.get_run_event_log_path())
        if event.event_type in TERMINAL_WORKFLOW_EVENT_TYPES
    ]
    assert [event.event_type for event in terminal_events] == [
        EventType.WORKFLOW_FAILED
    ]
    assert "- Status: failed" in output.get_run_summary_path().read_text(
        encoding="utf-8"
    )


def test_required_observer_stop_failure_prevents_terminal_commit(
    tmp_path: Path,
) -> None:
    plan = make_plan()
    console = Console(file=io.StringIO(), force_terminal=False, color_system=None)
    output = OutputManager("Workflow", base_dir=tmp_path, template_base_dir=tmp_path)
    context = WorkflowRunContext(
        config=Config(version=SCHEMA_VERSION, agents={}),
        source=PreflightWorkflowSource.from_workflow(
            WorkflowPlan(name=plan.workflow_name, nodes=[]),
        ),
        console=console,
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
    )
    components = RuntimeComponents(
        artifact_store=output,
        base_invoker=object(),
        observers=(_RequiredStopFailureObserver(),),
        suppress_progress_output=False,
    )
    warning_recorder = WorkflowWarningRecorder(
        workflow=context.workflow,
        console=console,
    )
    output.write_run_manifest(
        make_run_manifest(output.run_id, output.run_key_name, status="running")
    )
    terminalization = TerminalizationCoordinator(
        output=output,
        workflow_name=plan.workflow_name,
    )

    async def complete_execution(
        *args: object,  # noqa: ARG001 - Required by execution test double.
        **kwargs: object,  # noqa: ARG001 - Required by execution test double.
    ) -> None:
        return None

    with pytest.raises(RuntimeError, match="required observer stop failed"):
        asyncio.run(
            execution_module.run_and_finalize_workflow(
                context=context,
                output=output,
                components=components,
                plan=plan,
                secret_context=SecretContext(),
                execute_workflow_impl=complete_execution,
                warning_recorder=warning_recorder,
                observability_hub_cls=None,
                workflow_identity=".crewplane/workflows/workflow.task.md",
                terminalization=terminalization,
            )
        )

    assert terminalization.manifest_published is False
    assert terminalization.event_published is True
    assert terminalization.result_published is True
    assert terminalization.summary_published is True
    assert terminalization.committed is False
    manifest = RunManifest.model_validate_json(
        (output.stages_dir / "manifests" / "run.json").read_text(encoding="utf-8")
    )
    assert manifest.status == "running"
