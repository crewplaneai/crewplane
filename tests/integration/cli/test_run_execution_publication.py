from __future__ import annotations

import asyncio
import io
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from crewplane.artifacts.locks import SameContextLock, policy
from crewplane.artifacts.manager import OutputManager
from crewplane.artifacts.memory.completion import read_completion
from crewplane.cli.run import completion, execution
from crewplane.cli.run.completion import RunArtifactLocations
from crewplane.cli.run.terminalization import TerminalizationCoordinator
from crewplane.core.config import MemorySettings
from crewplane.core.platform import is_native_windows
from crewplane.core.preflight.source import PreflightWorkflowSource
from crewplane.runtime.agent.process.drain import (
    ProcessDrainError,
    ProcessDrainEvidence,
    unconfirmed_process_cleanup,
)
from crewplane.runtime.execution import execute_workflow
from scripts.validation_policy import load_policy
from tests.helpers.memory import initialize_memory
from tests.helpers.platforms import requires_project_memory
from tests.integration.cli.workflow_runner_support import (
    mock_runner_config,
    run_directories,
    runner_workflow,
)

TIMEOUTS = load_policy().timeouts


@pytest.mark.race
@pytest.mark.parametrize(
    "windows_lock_policy", [is_native_windows(), True], ids=["native", "windows"]
)
@pytest.mark.parametrize(
    ("phase", "expected_status"),
    [
        ("allocation", None),
        ("lock_update", None),
        ("preflight", None),
        ("initial_manifest", "cancelled"),
        ("logger", "cancelled"),
        ("terminal_views", "succeeded"),
        ("failed_terminal_views", "failed"),
        ("failed_terminal_manifest", "failed"),
        ("failed_release", "failed"),
        ("setup_failure", "failed"),
        ("terminal_manifest", "succeeded"),
        ("release", "succeeded"),
        pytest.param("completion", "succeeded", marks=requires_project_memory),
    ],
)
def test_cancellation_drains_publication_and_finishes_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    expected_status: str | None,
    windows_lock_policy: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(policy, "is_native_windows", lambda: windows_lock_policy)
    target, name = {
        "allocation": (execution, "allocate_run_output"),
        "lock_update": (SameContextLock, "update_run"),
        "preflight": (execution, "materialize_preflight_success"),
        "initial_manifest": (execution, "write_initial_run_manifest"),
        "logger": (execution, "PersistentRunLogger"),
        "terminal_views": (TerminalizationCoordinator, "commit"),
        "failed_terminal_views": (TerminalizationCoordinator, "commit"),
        "failed_terminal_manifest": (OutputManager, "update_run_manifest_status"),
        "failed_release": (SameContextLock, "release"),
        "setup_failure": (execution, "terminalize_component_setup_failure"),
        "terminal_manifest": (OutputManager, "update_run_manifest_status"),
        "release": (SameContextLock, "release"),
        "completion": (completion, "save_completion"),
    }[phase]
    original = getattr(target, name)
    release_worker = threading.Event()
    locations = RunArtifactLocations()
    workflow = runner_workflow()
    config = mock_runner_config()
    if phase == "completion":
        initialize_memory(tmp_path)
        workflow.nodes[0].memory = True
        config.settings.memory = MemorySettings(extractor="alpha")
    console_output = io.StringIO()
    failure = ProcessDrainError(
        ProcessDrainEvidence(123, 123, True, False), "process cleanup unconfirmed"
    )
    execution_failure_phases = {
        "failed_terminal_views",
        "failed_terminal_manifest",
        "failed_release",
    }

    async def fail_execution(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise failure

    if phase == "setup_failure":

        def fail_components(*args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            raise failure

        monkeypatch.setattr(execution, "build_components_for_run", fail_components)

    async def check() -> None:
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        stopped = threading.Event()

        def blocked_publication(*args: Any, **kwargs: Any) -> Any:
            assert threading.current_thread() is not threading.main_thread()
            loop.call_soon_threadsafe(started.set)
            try:
                assert release_worker.wait(5), "publication worker was not released"
                return original(*args, **kwargs)
            finally:
                stopped.set()

        monkeypatch.setattr(target, name, blocked_publication)
        task = asyncio.create_task(
            execution.execute_workflow_run(
                config,
                PreflightWorkflowSource.from_workflow(
                    workflow, root_workflow_path=tmp_path / "workflow.task.md"
                ),
                False,
                True,
                Console(file=console_output, force_terminal=False),
                execute_workflow_impl=(
                    fail_execution
                    if phase in execution_failure_phases
                    else execute_workflow
                ),
                artifact_locations=locations,
            )
        )
        readiness = asyncio.create_task(started.wait())
        try:
            await asyncio.wait_for(
                asyncio.wait({task, readiness}, return_when=asyncio.FIRST_COMPLETED),
                5,
            )
            assert started.is_set(), "publication did not reach its worker"
            lock_dirs = list((tmp_path / ".crewplane/locks").iterdir())
            assert len(lock_dirs) == (0 if phase == "completion" else 1)
            for cancellation_count in range(1, 3):
                task.cancel()
                await asyncio.sleep(0)
                assert task.cancelling() == cancellation_count
                assert not task.done()
                assert not stopped.is_set()
                assert all(lock_dir.is_dir() for lock_dir in lock_dirs)
            release_worker.set()
            with pytest.raises(asyncio.CancelledError) as raised:
                await asyncio.wait_for(task, 5)
            assert stopped.is_set()
            cleanup_unconfirmed = (
                phase in execution_failure_phases or phase == "setup_failure"
            )
            if cleanup_unconfirmed:
                assert unconfirmed_process_cleanup(raised.value) is failure
                assert locations.cleanup_confirmed is False
            if windows_lock_policy and cleanup_unconfirmed:
                assert locations.lock_released is False
                assert list((tmp_path / ".crewplane/locks").iterdir()) == lock_dirs
                assert all(lock_dir.is_dir() for lock_dir in lock_dirs)
            else:
                assert locations.lock_released is True
                assert not list((tmp_path / ".crewplane/locks").iterdir())
            (run_dir,) = run_directories(tmp_path)
            manifest_path = run_dir / "manifests/run.json"
            if expected_status is None:
                assert not manifest_path.exists()
            else:
                manifest = json.loads(manifest_path.read_bytes())
                assert manifest["status"] == expected_status
                summary = (run_dir / "logs/summary.md").read_text()
                assert f"- Status: {expected_status}" in summary
                if phase != "setup_failure":
                    assert f"Status: {expected_status}" in console_output.getvalue()
            if phase == "completion":
                assert read_completion(run_dir).eligible
        finally:
            release_worker.set()
            task.cancel()
            readiness.cancel()
            await asyncio.gather(task, readiness, return_exceptions=True)

    asyncio.run(check())


@pytest.mark.race
@pytest.mark.parametrize(
    "phase", ["acquire_same_context_lock", "write_initial_run_manifest"]
)
def test_runner_shutdown_settles_publication_before_releasing_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    monkeypatch.chdir(tmp_path)
    release = threading.Event()
    finished = threading.Event()
    locations = RunArtifactLocations()
    cancellations: list[asyncio.CancelledError] = []
    cleanup: list[bool] = []
    original = getattr(execution, phase)

    async def main() -> None:
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()

        def blocked_publication(*args: Any, **kwargs: Any) -> Any:
            acquired = (
                original(*args, **kwargs)
                if phase == "acquire_same_context_lock"
                else None
            )
            loop.call_soon_threadsafe(entered.set)
            try:
                assert release.wait(TIMEOUTS.scenario_seconds), "worker not released"
                return (
                    acquired
                    if phase == "acquire_same_context_lock"
                    else original(*args, **kwargs)
                )
            finally:
                finished.set()

        monkeypatch.setattr(execution, phase, blocked_publication)

        async def owner() -> None:
            try:
                await execution.execute_workflow_run(
                    mock_runner_config(),
                    PreflightWorkflowSource.from_workflow(
                        runner_workflow(),
                        root_workflow_path=tmp_path / "workflow.task.md",
                    ),
                    False,
                    True,
                    Console(file=io.StringIO(), force_terminal=False),
                    artifact_locations=locations,
                )
            except asyncio.CancelledError as exc:
                cancellations.append(exc)
            finally:
                cleanup.append(finished.is_set())

        async def release_on_shutdown() -> None:
            try:
                await asyncio.Future()
            finally:
                release.set()

        tasks = [
            asyncio.create_task(owner()),
            asyncio.create_task(release_on_shutdown()),
        ]
        await asyncio.wait_for(entered.wait(), TIMEOUTS.readiness_seconds)
        assert all(not task.done() for task in tasks)

    try:
        asyncio.run(main())
    finally:
        release.set()

    assert len(cancellations) == 1
    assert cleanup == [True]
    assert locations.lock_released is True
    assert locations.cleanup_confirmed is True
    assert not list((tmp_path / ".crewplane/locks").iterdir())
    runs = run_directories(tmp_path)
    if phase == "acquire_same_context_lock":
        assert runs == []
    else:
        (run_dir,) = runs
        manifest = json.loads((run_dir / "manifests/run.json").read_bytes())
        assert manifest["status"] == "cancelled"
        assert manifest["cancel_reason"] == "external_cancellation"
        assert "- Status: cancelled" in (run_dir / "logs/summary.md").read_text()
