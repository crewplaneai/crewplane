from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event

import pytest

from crewplane.architecture.contracts import EventType, ExecutionStatus
from crewplane.artifacts import OutputManager
from crewplane.artifacts.atomic import atomic_write_json
from crewplane.artifacts.naming import (
    node_state_relative_path,
    review_checkpoint_relative_path,
)
from crewplane.artifacts.resume.checkpoint_hydration import hydrate_review_checkpoints
from crewplane.artifacts.resume.validation import validate_resume_frontier
from crewplane.core.config import AgentConfig, Config, Settings
from crewplane.core.review_checkpoint import OpenReviewCheckpoint
from crewplane.core.workflow.models import WorkflowNode, WorkflowPlan
from crewplane.runtime.execution.activity.telemetry import (
    ExecutionTelemetry,
    RuntimeActivityTracker,
)
from crewplane.runtime.execution.errors import (
    NodeExecutionError,
    WorkflowExecutionError,
)
from crewplane.runtime.execution.review_loop import checkpoint
from crewplane.runtime.execution.sequential import execute_sequential_stage
from crewplane.runtime.execution.workflow.execution_session import (
    initialize_workflow_execution,
)
from crewplane.runtime.execution.workflow.scheduling import (
    finalize_execution,
    run_scheduling_loop,
)
from crewplane.version import SCHEMA_VERSION
from tests.helpers.review_checkpoints import (
    checkpoint_run,
    observation,
    open_checkpoint,
    prepare_checkpoint_hydration,
    write_manifest,
)
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    MockAgentInvoker,
    compile_test_plan,
    review_output,
)


def test_entry_rejection_fails_only_the_resumed_node(tmp_path: Path) -> None:
    async def run():
        output = OutputManager("Checkpoint", base_dir=tmp_path)
        config = Config(
            version=SCHEMA_VERSION,
            agents={"alpha": AgentConfig(cli_cmd=["mock"], default_model="alpha")},
            settings=Settings(max_concurrent_nodes=2),
        )
        workflow = WorkflowPlan(
            name=output.task_name,
            nodes=[
                WorkflowNode.model_validate(
                    {
                        "id": node_id,
                        "mode": "sequential",
                        "needs": ["review"] if node_id == "dependent" else [],
                        "prompt_segments": [{"role": "shared", "content": "Task"}],
                        "providers": [
                            {"provider": "alpha", "role": role}
                            for role in (
                                ("executor", "reviewer")
                                if node_id == "review"
                                else ("executor",)
                            )
                        ],
                    }
                )
                for node_id in ("review", "independent", "dependent")
            ],
        )
        _, runtime = compile_test_plan(config, workflow, output)
        runtime.workflow_identity = "checkpoint.task.md"
        tracker = RuntimeActivityTracker()
        tracker.mark_node_running("review")
        await execute_sequential_stage(
            runtime.plan.nodes[0],
            output,
            runtime,
            MockAgentInvoker(["candidate", review_output(verdict="NO_FINDINGS")]),
            ExecutionTelemetry(
                runtime.plan.workflow_name, output.run_id, activity_tracker=tracker
            ),
        )
        frontier, fresh, restored = prepare_checkpoint_hydration(output, runtime)
        hydrate_review_checkpoints(frontier, restored.plan, fresh)
        rejected = asyncio.Event()

        def capture(event):
            if (
                event.event_type == EventType.NODE_FAILED
                and event.context.node_id == "review"
            ):
                rejected.set()

        class WaitingInvoker(MockAgentInvoker):
            async def invoke(self, *args, **kwargs):
                assert kwargs["invocation_context"].node_id == "independent"
                await rejected.wait()
                await super().invoke(*args, **kwargs)

        session = initialize_workflow_execution(
            restored.plan,
            fresh,
            restored.secret_context,
            capture,
            fresh.run_id,
            True,
            "checkpoint.task.md",
            (),
        )
        (tmp_path / "changed.txt").write_text("changed after runtime setup")
        invoker = WaitingInvoker(["independent completed"])
        try:
            async with asyncio.timeout(5):
                await run_scheduling_loop(session, fresh, invoker)
                with pytest.raises(WorkflowExecutionError, match="blocked: dependent"):
                    await finalize_execution(session)
            assert isinstance(session.state.node_errors["review"], NodeExecutionError)
            assert session.state.statuses == {
                "review": ExecutionStatus.FAILED,
                "independent": ExecutionStatus.SUCCEEDED,
                "dependent": ExecutionStatus.BLOCKED,
            }
            assert [call["node_id"] for call in invoker.calls] == ["independent"]
            assert (
                fresh.stages_dir / node_state_relative_path("independent")
            ).is_file()
        finally:
            session.runtime_context.runtime_publications.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "damage", ["active", "previous", "selected", "round", "coordinates", "feedback"]
)
def test_malformed_remediation_cursor_is_unavailable_before_hydration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    async def run():
        output, runtime = checkpoint_run(tmp_path)
        publish = output.write_review_checkpoint

        def stop(record):
            path = publish(record)
            if (
                isinstance(record, OpenReviewCheckpoint)
                and record.next_phase == "executors"
            ):
                raise RuntimeError("stop at remediation")
            return path

        monkeypatch.setattr(output, "write_review_checkpoint", stop)
        with pytest.raises(RuntimeError, match="stop at remediation"):
            await execute_sequential_stage(
                runtime.plan.nodes[0],
                output,
                runtime,
                MockAgentInvoker(
                    [
                        "candidate",
                        review_output(
                            major="- Fix the issue", verdict="CHANGES_REQUESTED"
                        ),
                    ]
                ),
            )
        payload = open_checkpoint(output).model_dump(mode="json")
        active = payload["progress"]["active_audit"]
        match damage:
            case "active":
                payload["progress"]["active_audit"] = None
            case "previous":
                active["previous_executor_outputs"] = None
            case "selected":
                active["selected_round_num"] = 0
            case "round":
                active["last_round_num"] = 2
            case "coordinates":
                payload["audit"] = 2
                payload["progress"]["executed_audit_rounds"] = 2
            case "feedback":
                active["previous_unresolved_fingerprints"] = []
        atomic_write_json(
            output.stages_dir / review_checkpoint_relative_path("review"), payload
        )
        assert not validate_resume_frontier(
            write_manifest(output, runtime, "failed"),
            runtime.plan,
            observation(runtime, output),
        ).checkpoints

    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_slow_project_observation_leaves_event_loop_and_publications_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    started, release, finished = Event(), Event(), Event()
    original = checkpoint.project_fingerprint

    def slow_observation(*args):
        started.set()
        try:
            assert release.wait(3), "checkpoint preparation blocked the event loop"
            return original(*args)
        finally:
            finished.set()

    monkeypatch.setattr(checkpoint, "project_fingerprint", slow_observation)

    async def run():
        output, runtime = checkpoint_run(tmp_path)
        task = asyncio.create_task(
            execute_sequential_stage(
                runtime.plan.nodes[0],
                output,
                runtime,
                MockAgentInvoker(["candidate", review_output(verdict="NO_FINDINGS")]),
            )
        )
        try:
            assert await asyncio.to_thread(started.wait, 3)
            assert not task.done()
            assert runtime.runtime_publications.snapshot()[0]
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert output.read_review_checkpoint("review") is None
            release.set()
            if not cancel:
                await task
                assert open_checkpoint(output).next_phase == "finalize"
            assert await asyncio.to_thread(finished.wait, 3)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        if cancel:
            assert output.read_review_checkpoint("review") is None

    asyncio.run(run())
