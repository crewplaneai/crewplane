from __future__ import annotations

import asyncio
import io
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from crewplane.artifacts.resume.decision import ResumeDecision
from crewplane.artifacts.resume.validation import ValidatedResumeFrontier
from crewplane.artifacts.run_history import RunHistoryRecord
from crewplane.cli.run import execution, execution_resume
from crewplane.cli.run.completion import RunArtifactLocations
from crewplane.cli.run.resume import ResumePlan
from crewplane.core.preflight.source import PreflightWorkflowSource
from crewplane.observability.events import read_event_log
from tests.helpers.platforms import requires_tmux_support
from tests.helpers.resume import WORKFLOW_IDENTITY, make_run_manifest
from tests.integration.cli.workflow_runner_support import (
    mock_runner_config,
    run_directories,
    runner_workflow,
)


def _resume_plan(tmp_path: Path, kind: str) -> ResumePlan:
    manifest = make_run_manifest("source", "workflow--source", status="failed")
    history = RunHistoryRecord(
        manifest=manifest,
        manifest_path=tmp_path / "history/manifests/run.json",
        run_dir=tmp_path / "history",
        results_dir=tmp_path / "results",
    )
    if kind == "dedupe":
        return ResumePlan(
            WORKFLOW_IDENTITY,
            ResumeDecision(kind="skip", successful_run=history),
        )
    return ResumePlan(
        WORKFLOW_IDENTITY,
        ResumeDecision(kind="resume", resume_source=history),
        ValidatedResumeFrontier(source=history, node_states={}),
    )


@pytest.mark.parametrize(
    "phase", ["expansions", "results", "checkpoints", "components"]
)
def test_setup_failure_publishes_failure_before_releasing_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    monkeypatch.chdir(tmp_path)
    resume = _resume_plan(tmp_path, "resume")

    def select_resume(*args: Any) -> ResumePlan:
        del args
        return resume

    monkeypatch.setattr(execution, "prepare_run_resume", select_resume)
    failure = RuntimeError(f"{phase} setup failed")
    target, name = {
        "expansions": (execution, "hydrate_expansions"),
        "results": (execution_resume, "hydrate_resume_frontier"),
        "checkpoints": (execution_resume, "hydrate_review_checkpoints"),
        "components": (execution, "build_components_for_run"),
    }[phase]

    def fail_setup(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise failure

    monkeypatch.setattr(target, name, fail_setup)
    with pytest.raises(RuntimeError) as raised:
        asyncio.run(
            execution.execute_workflow_run(
                mock_runner_config(),
                PreflightWorkflowSource.from_workflow(
                    runner_workflow(), root_workflow_path=tmp_path / "workflow.task.md"
                ),
                False,
                True,
                Console(file=io.StringIO(), force_terminal=False),
            )
        )
    assert raised.value is failure
    (run_dir,) = run_directories(tmp_path)
    manifest = json.loads((run_dir / "manifests/run.json").read_bytes())
    assert manifest["status"] == "failed"
    assert manifest["failure_message"] == str(failure)
    assert [
        event.event_type for event in read_event_log(run_dir / "logs/events.ndjson")
    ] == ["workflow_failed"]
    assert "- Status: failed" in (run_dir / "logs/summary.md").read_text()
    assert not list((tmp_path / ".crewplane/locks").iterdir())


@requires_tmux_support
@pytest.mark.race
@pytest.mark.parametrize("lookup_fails", [False, True])
def test_cancellation_drains_live_component_lookup_before_terminalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lookup_fails: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    config = mock_runner_config()
    config.settings.integrations.ui.implementation = "tmux"
    locations = RunArtifactLocations()
    console_output = io.StringIO()
    release_lookup = threading.Event()
    lookup_error = RuntimeError("tmux lookup failed")

    async def check() -> None:
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        stopped = threading.Event()

        def blocked_lookup(executable: str) -> None:
            assert executable == "tmux"
            assert threading.current_thread() is not threading.main_thread()
            loop.call_soon_threadsafe(started.set)
            try:
                assert release_lookup.wait(5), "lookup worker was not released"
                if lookup_fails:
                    raise lookup_error
            finally:
                stopped.set()

        task = asyncio.create_task(
            execution.execute_workflow_run(
                config,
                PreflightWorkflowSource.from_workflow(
                    runner_workflow(), root_workflow_path=tmp_path / "workflow.task.md"
                ),
                False,
                False,
                Console(file=console_output, force_terminal=True, color_system=None),
                which_fn=blocked_lookup,
                artifact_locations=locations,
            )
        )
        readiness = asyncio.create_task(started.wait())
        try:
            await asyncio.wait_for(
                asyncio.wait({task, readiness}, return_when=asyncio.FIRST_COMPLETED),
                5,
            )
            assert started.is_set(), "live component lookup did not reach its worker"
            (run_dir,) = run_directories(tmp_path)
            manifest_path = run_dir / "manifests/run.json"
            (lock_dir,) = (tmp_path / ".crewplane/locks").iterdir()
            for cancellation_count in range(1, 3):
                task.cancel()
                await asyncio.sleep(0)
                assert task.cancelling() == cancellation_count
                assert not task.done()
                assert not stopped.is_set()
                assert lock_dir.is_dir()
                assert json.loads(manifest_path.read_bytes())["status"] == "running"
                assert not (run_dir / "logs/summary.md").exists()
            release_lookup.set()
            with pytest.raises(asyncio.CancelledError) as raised:
                await asyncio.wait_for(task, 5)
            assert stopped.is_set()
            if lookup_fails:
                assert raised.value.__cause__ is lookup_error
            manifest = json.loads(manifest_path.read_bytes())
            assert manifest["status"] == "cancelled"
            assert manifest["cancel_reason"] == "external_cancellation"
            assert [
                event.event_type
                for event in read_event_log(run_dir / "logs/events.ndjson")
                if event.event_type.startswith("workflow_")
            ] == ["workflow_cancelled"]
            assert "- Status: cancelled" in (run_dir / "logs/summary.md").read_text()
            assert "Status: cancelled" in console_output.getvalue()
            assert locations.lock_released is True
            assert locations.cleanup_confirmed is True
            assert not lock_dir.exists()
        finally:
            release_lookup.set()
            task.cancel()
            readiness.cancel()
            await asyncio.gather(task, readiness, return_exceptions=True)

    asyncio.run(check())


@pytest.mark.race
@pytest.mark.parametrize("worker_fails", [False, True])
@pytest.mark.parametrize(
    "phase",
    [
        "lock",
        "resume",
        "dedupe",
        "history",
        "hydration",
        "results",
        "checkpoints",
        "exports",
    ],
)
def test_cancellation_drains_blocking_run_phase_before_releasing_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    worker_fails: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    target_name = {
        "lock": "acquire_same_context_lock",
        "resume": "build_resume_plan",
        "dedupe": "handle_duplicate_skip",
        "history": "fulfill_historical_branch_exports",
        "hydration": "hydrate_expansions",
        "results": "hydrate_resume_frontier",
        "checkpoints": "hydrate_review_checkpoints",
        "exports": "fulfill_branch_exports",
    }[phase]
    if phase in {"dedupe", "history", "hydration", "results", "checkpoints"}:
        plan = _resume_plan(tmp_path, phase)

        def select_resume(*args: Any, **kwargs: Any) -> ResumePlan:
            del args, kwargs
            return plan

        def reconcile_history(*args: Any, **kwargs: Any) -> None:
            del args, kwargs

        monkeypatch.setattr(execution_resume, "build_resume_plan", select_resume)
        monkeypatch.setattr(
            execution_resume, "fulfill_historical_branch_exports", reconcile_history
        )
    target = (
        execution if phase in {"lock", "hydration", "exports"} else execution_resume
    )
    original = getattr(target, target_name)
    release_worker = threading.Event()
    worker_error = RuntimeError(f"{phase} worker failed")
    locations = RunArtifactLocations()

    async def check() -> None:
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        worker_stopped = threading.Event()

        def blocked_phase(*args: Any, **kwargs: Any) -> Any:
            assert threading.current_thread() is not threading.main_thread()
            result = (
                None if phase == "lock" and worker_fails else original(*args, **kwargs)
            )
            loop.call_soon_threadsafe(started.set)
            try:
                assert release_worker.wait(5), "run owner did not release test worker"
                if worker_fails:
                    raise worker_error
                return result
            finally:
                worker_stopped.set()

        monkeypatch.setattr(target, target_name, blocked_phase)
        task = asyncio.create_task(
            execution.execute_workflow_run(
                config=mock_runner_config(),
                source=PreflightWorkflowSource.from_workflow(
                    runner_workflow(), root_workflow_path=tmp_path / "workflow.task.md"
                ),
                force=False,
                no_live=True,
                console=Console(file=io.StringIO(), force_terminal=False),
                artifact_locations=locations,
            )
        )
        readiness = asyncio.create_task(started.wait())
        try:
            await asyncio.wait_for(
                asyncio.wait({task, readiness}, return_when=asyncio.FIRST_COMPLETED),
                5,
            )
            assert started.is_set(), "blocking run phase executed on the event loop"
            locks_root = tmp_path / ".crewplane/locks"
            lock_dirs = list(locks_root.iterdir()) if locks_root.exists() else []
            assert len(lock_dirs) == (0 if phase == "lock" and worker_fails else 1)
            for cancellation_count in range(1, 3):
                task.cancel()
                await asyncio.sleep(0)
                assert task.cancelling() == cancellation_count
                assert not task.done()
                assert all(lock_dir.is_dir() for lock_dir in lock_dirs)
                assert not worker_stopped.is_set()
            release_worker.set()
            with pytest.raises(asyncio.CancelledError) as raised:
                await asyncio.wait_for(task, 5)
            assert worker_stopped.is_set()
            if worker_fails:
                assert raised.value.__cause__ is worker_error
            runs = run_directories(tmp_path)
            if phase == "lock" and worker_fails:
                assert locations.lock_released is None
                assert runs == []
                return
            (lock_dir,) = lock_dirs
            assert not lock_dir.exists()
            assert locations.lock_released is True
            if phase in {"hydration", "results", "checkpoints", "exports"}:
                (run_dir,) = runs
                manifest = json.loads((run_dir / "manifests/run.json").read_bytes())
                assert manifest["status"] == "cancelled"
                assert manifest["cancel_reason"] == "external_cancellation"
                terminal_events = [
                    event.event_type
                    for event in read_event_log(run_dir / "logs/events.ndjson")
                    if event.event_type.startswith("workflow_")
                    and event.event_type != "workflow_started"
                ]
                assert terminal_events == ["workflow_cancelled"]
                assert (
                    "- Status: cancelled" in (run_dir / "logs/summary.md").read_text()
                )
            else:
                assert runs == []
        finally:
            release_worker.set()
            task.cancel()
            readiness.cancel()
            await asyncio.gather(task, readiness, return_exceptions=True)

    asyncio.run(check())
