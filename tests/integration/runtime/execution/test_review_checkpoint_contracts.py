from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from crewplane.artifacts import OutputManager
from crewplane.artifacts.naming import review_checkpoint_relative_path
from crewplane.artifacts.resume.checkpoint_hydration import hydrate_review_checkpoints
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.review_checkpoint import OpenReviewCheckpoint
from crewplane.core.review_checkpoint_state import (
    CheckpointGeneratedMapping,
    CheckpointPhase,
    CheckpointProjectObservation,
)
from crewplane.runtime.execution.activity.telemetry import (
    ExecutionTelemetry,
    RuntimeActivityTracker,
)
from crewplane.runtime.execution.review_loop import checkpoint, orchestration
from crewplane.runtime.execution.review_loop.types import (
    ReviewLoopProgress,
    ReviewLoopRunContext,
)
from crewplane.runtime.execution.runtime_context import CompiledRuntimeContext
from crewplane.runtime.execution.sequential import execute_sequential_stage
from tests.helpers.resume import make_plan
from tests.helpers.review_checkpoints import (
    checkpoint_payload,
    checkpoint_run,
    open_checkpoint,
    prepare_checkpoint_hydration,
)
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    MockAgentInvoker,
    review_output,
)


@pytest.fixture
def observation_context(tmp_path: Path) -> ReviewLoopRunContext:
    output = OutputManager("Checkpoint", base_dir=tmp_path)
    plan = make_plan(review_loop=True).model_copy(
        update={"project_root": str(tmp_path)}
    )
    runtime = CompiledRuntimeContext(plan, SecretContext())
    return ReviewLoopRunContext(
        runtime,
        plan.nodes[0],
        output,
        output.stages_dir / "a",
        MockAgentInvoker([]),
        None,
        (),
        (),
        "",
        "",
        2,
        3,
    )


@pytest.mark.parametrize(
    "activity", ["untracked", "exclusive", "busy", "changed", "unreadable"]
)
def test_observation_order_and_reliability_rules(
    observation_context: ReviewLoopRunContext,
    monkeypatch: pytest.MonkeyPatch,
    activity: str,
) -> None:
    context = observation_context
    events: list[str] = []
    tracker = RuntimeActivityTracker()
    snapshot = tracker.snapshot

    def observe_activity(node_id: str):
        events.append("snapshot")
        return snapshot(node_id)

    def fingerprint(root: Path, artifact_roots: tuple[Path, ...]) -> str | None:
        assert root == Path(context.runtime_context.plan.project_root)
        assert artifact_roots == (context.output.stages_dir.parent.parent,)
        events.append("fingerprint")
        if activity == "changed":
            tracker.mark_node_running("b")
            tracker.mark_node_finished("b")
        return None if activity == "unreadable" else "b" * 64

    monkeypatch.setattr(checkpoint, "project_fingerprint", fingerprint)
    monkeypatch.setattr(tracker, "snapshot", observe_activity)
    if activity != "untracked":
        context.telemetry = ExecutionTelemetry(
            "Checkpoint", context.output.run_id, activity_tracker=tracker
        )
    if activity == "busy":
        tracker.mark_node_running("b")
    marker = OpenReviewCheckpoint.model_validate(checkpoint_payload())
    context.runtime_context.review_checkpoints["a"] = marker

    boundary = checkpoint.boundary_observation(context)
    assert boundary is not None
    assert boundary.reliable == (activity == "exclusive")
    entry_reliable = activity in {"untracked", "exclusive"}
    if entry_reliable:
        checkpoint.require_entry_project(
            context.runtime_context, context.output, context.stage, context.telemetry
        )
    else:
        with pytest.raises(
            ValueError, match="Project contents changed or cannot be verified"
        ):
            checkpoint.require_entry_project(
                context.runtime_context,
                context.output,
                context.stage,
                context.telemetry,
            )
    observation_order = (
        ["fingerprint"]
        if activity == "untracked"
        else ["snapshot", "fingerprint", "snapshot"]
    )
    assert events == observation_order * 2
    assert context.runtime_context.review_checkpoints["a"] is marker


@pytest.mark.parametrize(
    "limit,single_node,expected", [(1, False, True), (2, True, True), (2, False, False)]
)
def test_untracked_boundary_uses_scheduling_limits(
    observation_context: ReviewLoopRunContext,
    monkeypatch: pytest.MonkeyPatch,
    limit: int,
    single_node: bool,
    expected: bool,
) -> None:
    runtime = observation_context.runtime_context
    runtime.plan.runtime_config_snapshot["execution"] = {"max_concurrent_nodes": limit}
    if single_node:
        runtime.plan.execution_order = ["a"]

    def fingerprint(root: Path, artifact_roots: tuple[Path, ...]) -> str:
        del root, artifact_roots
        return "b" * 64

    monkeypatch.setattr(checkpoint, "project_fingerprint", fingerprint)
    result = checkpoint.boundary_observation(observation_context)
    assert result == CheckpointProjectObservation(
        fingerprint="b" * 64, reliable=expected
    )


@pytest.fixture
def completed_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[ReviewLoopRunContext, ReviewLoopProgress]]:
    output, runtime = checkpoint_run(tmp_path)
    captured: list[tuple[ReviewLoopRunContext, ReviewLoopProgress]] = []
    commit = orchestration.commit_checkpoint

    async def capture(
        context: ReviewLoopRunContext,
        progress: ReviewLoopProgress,
        phase: CheckpointPhase,
        local_round: int,
    ) -> None:
        await commit(context, progress, phase, local_round)
        if phase == "finalize":
            captured.append((context, progress))

    monkeypatch.setattr(orchestration, "commit_checkpoint", capture)
    with closing(runtime.runtime_publications):
        asyncio.run(
            execute_sequential_stage(
                runtime.plan.nodes[0],
                output,
                runtime,
                MockAgentInvoker(["candidate", review_output(verdict="NO_FINDINGS")]),
            )
        )
        assert len(captured) == 1
        yield captured[0]


def test_publication_finishes_before_pending_cancellation(
    completed_review: tuple[ReviewLoopRunContext, ReviewLoopProgress],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, progress = completed_review
    write = context.output.write_review_checkpoint
    progress.next_phase, progress.cursor_round = "executors", 99

    def request_cancellation(marker):
        task = asyncio.current_task()
        assert task is not None
        asyncio.get_running_loop().call_soon(task.cancel)
        return write(marker)

    monkeypatch.setattr(context.output, "write_review_checkpoint", request_cancellation)

    async def run() -> None:
        await checkpoint.commit_checkpoint(
            context, progress, "finalize", progress.last_round_num
        )
        marker = context.output.read_review_checkpoint(context.stage.id)
        assert marker == context.runtime_context.review_checkpoints[context.stage.id]
        assert (progress.next_phase, progress.cursor_round) == (
            "finalize",
            progress.last_round_num,
        )
        path = context.output.stages_dir / review_checkpoint_relative_path(
            context.stage.id
        )
        assert path in context.runtime_context.runtime_publications.snapshot()[0]
        with pytest.raises(asyncio.CancelledError):
            await asyncio.sleep(0)

    asyncio.run(run())


def test_missing_publication_preserves_marker_and_cursor(
    completed_review: tuple[ReviewLoopRunContext, ReviewLoopProgress],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, progress = completed_review
    runtime = context.runtime_context
    marker = runtime.review_checkpoints[context.stage.id]
    marker_path = context.output.stages_dir / review_checkpoint_relative_path(
        context.stage.id
    )
    before = marker_path.read_bytes()
    cursor = progress.next_phase, progress.cursor_round
    dependency = context.output.stages_dir / marker.progress.candidates()[0].output_path
    monkeypatch.setattr(runtime.runtime_publications, "snapshot", lambda: ({}, 0))

    with pytest.raises(ValueError) as error:
        asyncio.run(checkpoint.commit_checkpoint(context, progress, "reviewers", 1))

    assert (
        str(error.value)
        == f"Checkpoint output has no runtime publication: {dependency}"
    )
    assert marker_path.read_bytes() == before
    assert runtime.review_checkpoints[context.stage.id] is marker
    assert (progress.next_phase, progress.cursor_round) == cursor


@pytest.mark.parametrize("failure", ["project", "diagnostics", "publication"])
def test_restore_failure_preserves_completed_side_effects(
    completed_review: tuple[ReviewLoopRunContext, ReviewLoopProgress],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    context, _ = completed_review
    output, runtime = context.output, context.runtime_context
    marker = open_checkpoint(output)
    candidate = marker.progress.latest_executor_outputs[0]
    output.write_review_checkpoint(
        marker.model_copy(
            update={
                "generated_mappings": [
                    CheckpointGeneratedMapping(
                        output_path=candidate.output_path, snapshot_path=None
                    )
                ],
                "files": [
                    item
                    for item in marker.files
                    if item.purpose not in {"generated_file", "generated_metadata"}
                ],
            }
        )
    )
    frontier, fresh, restored = prepare_checkpoint_hydration(output, runtime)
    hydrate_review_checkpoints(frontier, restored.plan, fresh)
    marker = open_checkpoint(fresh)
    node = restored.plan.nodes[0]
    publications = restored.runtime_publications
    diagnostics = (
        fresh.stages_dir
        / node.artifact_contract.stage_path
        / "review-state/review-loop-status.json"
    )

    def fail(*args, **kwargs):
        del args, kwargs
        raise OSError("restore failed")

    if failure == "project":
        monkeypatch.setattr(checkpoint, "require_entry_project", fail)
    elif failure == "diagnostics":
        monkeypatch.setattr(checkpoint, "restore_diagnostics", fail)
    else:
        publish = publications.publish

        def publish_then_fail(*args, **kwargs):
            publish(*args, **kwargs)
            fail()

        monkeypatch.setattr(publications, "publish", publish_then_fail)
    with closing(publications), pytest.raises(OSError, match="^restore failed$"):
        checkpoint.restore_selected_checkpoints(restored, fresh)
    assert restored.review_checkpoints[node.id] == marker
    mappings = restored.generated_file_workspaces.roots_for_node(node.id)
    assert mappings == (
        {}
        if failure == "project"
        else {(fresh.stages_dir / candidate.output_path).resolve(): None}
    )
    assert diagnostics.exists() == (failure == "publication")
    published, _ = publications.snapshot()
    expected = (
        {}
        if failure != "publication"
        else {
            fresh.stages_dir / marker.files[0].relative_path: marker.files[0].signature
        }
    )
    assert published == expected
    assert open_checkpoint(fresh) == marker
