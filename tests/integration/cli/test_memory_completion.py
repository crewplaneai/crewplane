from __future__ import annotations

import asyncio
import json
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

import pytest

from crewplane.artifacts.locks import SameContextLock
from crewplane.artifacts.locks.ownership import read_owner
from crewplane.artifacts.memory.completion import read_completion
from crewplane.cli.memory.history import completed_parent
from crewplane.cli.run.completion import (
    CompletionDisplay,
    RunArtifactLocations,
    persist_memory_completion,
)
from crewplane.cli.run.execution import execute_workflow_run
from crewplane.cli.run.terminalization import TerminalizationCoordinator
from crewplane.core.preflight.source import PreflightWorkflowSource
from crewplane.runtime.agent.process.drain import (
    ProcessDrainError,
    ProcessDrainEvidence,
    unconfirmed_process_cleanup,
)
from crewplane.runtime.execution import execute_workflow
from tests.helpers.memory_processes import line, release, worker
from tests.helpers.memory_runtime import setup_parent
from tests.helpers.platforms import requires_lock_recovery, requires_project_memory
from tests.integration.cli.workflow_runner_support import runner_workflow

pytestmark = requires_project_memory


@requires_lock_recovery
@pytest.mark.race
def test_learning_handoff_uses_own_release_even_after_peer_reacquires_path(
    tmp_path: Path, monkeypatch
):
    original_release = SameContextLock.release
    original_commit = TerminalizationCoordinator.acknowledge_observer_shutdown
    original_display = CompletionDisplay.display
    ordering = []
    peer = None
    released_path = None
    with ExitStack() as stack:

        def acknowledge(coordinator):
            original_commit(coordinator)
            assert coordinator.committed
            ordering.append("committed-after-required-observer-shutdown")

        def release_and_reacquire(
            lock, terminal_complete=True, cleanup_unconfirmed=False
        ):
            nonlocal peer, released_path
            assert terminal_complete and not cleanup_unconfirmed
            owner = read_owner(lock.lock_dir)
            assert owner is not None
            released = original_release(lock, terminal_complete, cleanup_unconfirmed)
            assert released
            ordering.append("own-release")
            released_path = lock.lock_dir
            peer = stack.enter_context(
                worker(
                    tmp_path / ".crewplane",
                    "execution",
                    json.dumps(
                        {
                            "workflow_name": "Task",
                            "workflow_identity": owner.workflow_identity,
                            "workflow_signature": owner.workflow_signature,
                        }
                    ),
                )
            )
            assert line(peer) == "ready"
            assert line(peer) == "locked"
            ordering.append("peer-reacquired")
            return released

        def display(completion, console):
            assert ordering[-1] == "peer-reacquired"
            displayed = original_display(completion, console)
            ordering.append("displayed")
            return displayed

        monkeypatch.setattr(
            TerminalizationCoordinator, "acknowledge_observer_shutdown", acknowledge
        )
        monkeypatch.setattr(SameContextLock, "release", release_and_reacquire)
        monkeypatch.setattr(CompletionDisplay, "display", display)
        _config, completion, _packet, _fixtures, _console = setup_parent(tmp_path)
        assert completion.can_learn and completion.lock_released
        assert read_completion(completion.stages_dir).eligible
        assert released_path.is_dir()
        assert ordering == [
            "committed-after-required-observer-shutdown",
            "own-release",
            "peer-reacquired",
            "displayed",
        ]
        release(peer)
        assert line(peer) == "done"


@pytest.mark.parametrize(
    "condition",
    [
        "kind",
        "outcome",
        "manifest_committed",
        "cleanup_confirmed",
        "lock_released",
        "result_displayed",
    ],
)
def test_learning_requires_every_completed_run_prerequisite(tmp_path: Path, condition):
    _config, completion, _packet, _fixtures, _console = setup_parent(tmp_path)
    value = (
        "dedupe"
        if condition == "kind"
        else "cancelled"
        if condition == "outcome"
        else False
    )
    assert not replace(completion, **{condition: value}).can_learn


def test_historical_extraction_cannot_infer_cleanup_from_absent_posix_lock(
    tmp_path: Path,
):
    config, _parent, _packet, _fixtures, console = setup_parent(tmp_path)
    workflow = runner_workflow("Investigate protected reads.")
    workflow.nodes[0].memory = True
    source = PreflightWorkflowSource.from_workflow(
        workflow, root_workflow_path=tmp_path / "workflow.task.md"
    )
    failure = ProcessDrainError(
        ProcessDrainEvidence(123, 123, True, False), "descendant cleanup unconfirmed"
    )

    async def cleanup_failed(*args, **kwargs):
        await execute_workflow(*args, **kwargs)
        raise failure

    locations = RunArtifactLocations()
    with pytest.raises(Exception) as raised:
        asyncio.run(
            execute_workflow_run(
                config,
                source,
                True,
                True,
                console,
                execute_workflow_impl=cleanup_failed,
                project_root=tmp_path,
                artifact_locations=locations,
            )
        )
    assert unconfirmed_process_cleanup(raised.value) is failure
    assert not list((tmp_path / ".crewplane/locks").iterdir())
    receipt = read_completion(locations.stages_dir)
    assert not receipt.cleanup_confirmed and not receipt.eligible
    assert receipt.outcome == "failed"
    with pytest.raises(ValueError, match="completion proof"):
        completed_parent(tmp_path, locations.plan.run_key_name)


def test_completion_publication_failure_preserves_parent_and_disables_optional_learning(
    tmp_path: Path, monkeypatch
):
    _config, parent, _packet, _fixtures, console = setup_parent(tmp_path)
    (parent.stages_dir / "manifests/completion.json").unlink()

    def fail_save(*args):
        del args
        raise OSError("completion storage unavailable")

    monkeypatch.setattr("crewplane.cli.run.completion.save_completion", fail_save)
    result = persist_memory_completion(parent, console)
    assert not result.can_learn and result.outcome == parent.outcome == "succeeded"
    with pytest.raises(ValueError, match="completion proof is missing"):
        completed_parent(tmp_path, parent.plan.run_key_name)
