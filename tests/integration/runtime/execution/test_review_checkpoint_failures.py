from __future__ import annotations

import asyncio
import json
from contextlib import closing
from pathlib import Path
from threading import Event
from typing import Any, Literal

import pytest

from crewplane.architecture.contracts import EventType, ExecutionStatus
from crewplane.architecture.contracts.invocation_failures import InvocationFailureError
from crewplane.artifacts import OutputManager
from crewplane.artifacts.atomic import atomic_write_json
from crewplane.artifacts.naming import (
    node_state_relative_path,
    review_checkpoint_relative_path,
)
from crewplane.artifacts.results.review_loop_status import ReviewLoopStatusPayload
from crewplane.artifacts.resume.checkpoint_hydration import hydrate_review_checkpoints
from crewplane.artifacts.resume.validation import validate_resume_frontier
from crewplane.core.config import AgentConfig, Config, Settings
from crewplane.core.review_checkpoint import (
    ClosedReviewCheckpoint,
    OpenReviewCheckpoint,
)
from crewplane.core.workflow.models import WorkflowNode, WorkflowPlan
from crewplane.runtime.execution.activity.telemetry import (
    ExecutionTelemetry,
    RuntimeActivityTracker,
)
from crewplane.runtime.execution.errors import (
    NodeExecutionError,
    WorkflowExecutionError,
)
from crewplane.runtime.execution.review_loop import (
    audit_round,
    checkpoint,
    orchestration,
)
from crewplane.runtime.execution.review_loop.types import (
    AuditRoundProgress,
    AuditRoundRequest,
    CandidateValidationResult,
    ExecutorRoundArtifact,
)
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
from tests.integration.runtime.execution import review_loop_rounds_support
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    MockAgentInvoker,
    compile_test_plan,
    provider_failure,
    review_output,
)

# Bound hangs without making filesystem-heavy setup a cancellation deadline.
REVIEW_STAGE_TIMEOUT_SECONDS = 30


@pytest.mark.parametrize("consensus_on_exhaustion", ["fatal", "continue"])
@pytest.mark.parametrize("review_completed", [False, True])
def test_cancellation_during_completed_audit_status_preserves_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    consensus_on_exhaustion: Literal["fatal", "continue"],
    review_completed: bool,
) -> None:
    async def run() -> None:
        output, runtime = checkpoint_run(
            tmp_path, consensus_on_exhaustion, audit_rounds=1, depth=1
        )
        blocked = review_output(major="- Fix the bug.", verdict="CHANGES_REQUESTED")
        invoker = MockAgentInvoker(["candidate one", blocked, "candidate two", blocked])
        publish = orchestration.persist_review_loop_status
        loop = asyncio.get_running_loop()
        cancel_requested = False

        def cancel_during_status(
            node_dir: Path, payload: ReviewLoopStatusPayload
        ) -> Path:
            nonlocal cancel_requested
            if (
                payload["attempted_local_round_num"] == 2
                and payload["final_local_round_num"] == (2 if review_completed else 1)
                and not cancel_requested
            ):
                cancel_requested = True
                loop.call_soon_threadsafe(task.cancel, "completed audit publication")
            return publish(node_dir, payload)

        monkeypatch.setattr(
            orchestration, "persist_review_loop_status", cancel_during_status
        )
        fatal = consensus_on_exhaustion == "fatal" and review_completed
        error = orchestration.ReviewPolicyError if fatal else asyncio.CancelledError
        with closing(runtime.runtime_publications):
            task = asyncio.create_task(
                execute_sequential_stage(
                    runtime.plan.nodes[0], output, runtime, invoker
                )
            )
            with pytest.raises(error):
                await asyncio.wait_for(task, timeout=REVIEW_STAGE_TIMEOUT_SECONDS)
            assert cancel_requested
            assert [call["role"] for call in invoker.calls] == [
                "executor",
                "reviewer",
            ] * (2 if review_completed else 1)
            marker = output.read_review_checkpoint("review")
            if fatal:
                assert isinstance(marker, ClosedReviewCheckpoint)
                assert "failed to reach consensus" in marker.terminal_reason
            else:
                assert isinstance(marker, OpenReviewCheckpoint)
                assert (marker.next_phase, marker.local_round) == (
                    "reviewers" if review_completed else "executors",
                    2,
                )
            frontier = validate_resume_frontier(
                write_manifest(output, runtime, "failed" if fatal else "cancelled"),
                runtime.plan,
                observation(runtime, output),
            )
            assert frontier.checkpoint_node_ids == (() if fatal else ("review",))
            assert frontier.closed_checkpoint_ids == (
                frozenset({"review"}) if fatal else frozenset()
            )

    asyncio.run(run())


@pytest.mark.parametrize(
    "candidate",
    ["", "candidate", "updated candidate"],
    ids=["empty", "unchanged", "valid"],
)
@pytest.mark.parametrize(
    "scenario",
    [
        "fatal",
        "exhaustion_continue",
        "continue_on_failure",
        "earlier_round",
        "earlier_audit",
    ],
)
def test_cancellation_after_executor_validation_preserves_terminal_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate: str,
    scenario: str,
) -> None:
    async def run() -> None:
        output, runtime = checkpoint_run(
            tmp_path,
            "continue" if scenario == "exhaustion_continue" else "fatal",
            audit_rounds=2 if scenario == "earlier_audit" else 1,
            depth=2 if scenario == "earlier_round" else 1,
            continue_on_failure=scenario == "continue_on_failure",
        )
        blocked = review_output(major="- Fix the bug.", verdict="CHANGES_REQUESTED")
        invoker = MockAgentInvoker(["candidate", blocked, candidate])
        validate = audit_round.validate_executor_outputs
        loop = asyncio.get_running_loop()
        cancel_requested = False

        def cancel_after_validation(
            executor_outputs: list[ExecutorRoundArtifact],
        ) -> CandidateValidationResult:
            nonlocal cancel_requested
            result = validate(executor_outputs)
            if (
                invoker.calls[-1]["role"] == "executor"
                and invoker.calls[-1]["round_num"] == 2
            ):
                cancel_requested = True
                loop.call_soon_threadsafe(task.cancel, "completed executor validation")
            return result

        monkeypatch.setattr(
            audit_round, "validate_executor_outputs", cancel_after_validation
        )
        fatal = scenario == "fatal" and candidate != "updated candidate"
        expected_error = (
            orchestration.ReviewPolicyError if fatal else asyncio.CancelledError
        )
        with closing(runtime.runtime_publications):
            task = asyncio.create_task(
                execute_sequential_stage(
                    runtime.plan.nodes[0], output, runtime, invoker
                )
            )
            with pytest.raises(expected_error):
                await asyncio.wait_for(task, timeout=REVIEW_STAGE_TIMEOUT_SECONDS)
            assert cancel_requested
            assert [(call["role"], call["round_num"]) for call in invoker.calls] == [
                ("executor", 1),
                ("reviewer", 1),
                ("executor", 2),
            ]
            marker = output.read_review_checkpoint("review")
            if fatal:
                assert isinstance(marker, ClosedReviewCheckpoint)
                assert "failed to reach consensus" in marker.terminal_reason
            else:
                assert isinstance(marker, OpenReviewCheckpoint)
                assert (marker.next_phase, marker.local_round) == ("executors", 2)
            status = json.loads(
                (
                    output.stages_dir / "review/review-state/review-loop-status.json"
                ).read_text()
            )
            assert status["stop_reason"] == (
                "consensus_exhausted" if fatal else "cancelled"
            )
            frontier = validate_resume_frontier(
                write_manifest(output, runtime, "failed" if fatal else "cancelled"),
                runtime.plan,
                observation(runtime, output),
            )
            assert frontier.checkpoint_node_ids == (() if fatal else ("review",))
            assert frontier.closed_checkpoint_ids == (
                frozenset({"review"}) if fatal else frozenset()
            )

    asyncio.run(run())


@pytest.mark.parametrize("cancel_during_recovery", [False, True])
@pytest.mark.parametrize("recovery_check_failure", [False, True])
def test_executor_failure_survives_recovery_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_during_recovery: bool,
    recovery_check_failure: bool,
) -> None:
    async def run() -> None:
        output, runtime = checkpoint_run(tmp_path, audit_rounds=1, depth=1)
        failure = provider_failure("executor process failed")
        storage_error = OSError("recovery check failed")
        blocked = review_output(major="- Fix the bug.", verdict="CHANGES_REQUESTED")

        class FailingExecutor(MockAgentInvoker):
            async def invoke(self, *args: Any, **kwargs: Any) -> None:
                await super().invoke(*args, **kwargs)
                if (
                    self.calls[-1]["role"] == "executor"
                    and self.calls[-1]["round_num"] == 2
                ):
                    raise failure

        invoker = FailingExecutor(["candidate", blocked, "partial executor output"])
        recover = audit_round.recover_after_remediation_context_exhaustion
        loop = asyncio.get_running_loop()
        recovery_checked = False

        def cancel_after_recovery_check(
            request: AuditRoundRequest,
            progress: AuditRoundProgress,
            round_num: int,
            error: InvocationFailureError,
        ) -> bool:
            nonlocal recovery_checked
            assert error is failure
            recovered = recover(request, progress, round_num, error)
            assert not recovered
            recovery_checked = True
            if cancel_during_recovery:
                loop.call_soon_threadsafe(task.cancel, "recovery check cancellation")
            if recovery_check_failure:
                raise storage_error
            return recovered

        monkeypatch.setattr(
            audit_round,
            "recover_after_remediation_context_exhaustion",
            cancel_after_recovery_check,
        )
        expected = storage_error if recovery_check_failure else failure
        with closing(runtime.runtime_publications):
            task = asyncio.create_task(
                execute_sequential_stage(
                    runtime.plan.nodes[0], output, runtime, invoker
                )
            )
            with pytest.raises(type(expected)) as caught:
                await asyncio.wait_for(task, timeout=REVIEW_STAGE_TIMEOUT_SECONDS)
            assert caught.value is expected
            assert recovery_checked
            assert [(call["role"], call["round_num"]) for call in invoker.calls] == [
                ("executor", 1),
                ("reviewer", 1),
                ("executor", 2),
            ]
            marker = open_checkpoint(output)
            assert (marker.next_phase, marker.local_round) == ("executors", 2)
            status = json.loads(
                (
                    output.stages_dir / "review/review-state/review-loop-status.json"
                ).read_text()
            )
            assert status["stop_reason"] == "failed"
            frontier = validate_resume_frontier(
                write_manifest(output, runtime, "failed"),
                runtime.plan,
                observation(runtime, output),
            )
            assert frontier.checkpoint_node_ids == ("review",)
            assert not frontier.closed_checkpoint_ids

    asyncio.run(run())


@pytest.mark.parametrize(
    "scenario",
    [
        "fatal",
        "fatal_before_depth_limit",
        "earlier_audit",
        "exhaustion_continue",
        "continue_on_failure",
    ],
)
def test_cancellation_after_context_recovery_preserves_terminal_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    async def run() -> None:
        output, runtime = checkpoint_run(
            tmp_path,
            "continue" if scenario == "exhaustion_continue" else "fatal",
            audit_rounds=2 if scenario == "earlier_audit" else 1,
            depth=2 if scenario == "fatal_before_depth_limit" else 1,
            continue_on_failure=scenario == "continue_on_failure",
        )
        failure = review_loop_rounds_support.provider_failure(
            "provider_session_context_exhausted", "provider_session"
        )
        blocked = review_output(major="- Fix the bug.", verdict="CHANGES_REQUESTED")

        class ExhaustingExecutor(MockAgentInvoker):
            async def invoke(self, *args: Any, **kwargs: Any) -> None:
                await super().invoke(*args, **kwargs)
                if (
                    self.calls[-1]["role"] == "executor"
                    and self.calls[-1]["round_num"] == 2
                ):
                    raise failure

        invoker = ExhaustingExecutor(["candidate", blocked, "partial executor output"])
        recover = audit_round.recover_after_remediation_context_exhaustion
        loop = asyncio.get_running_loop()
        recovery_completed = False

        def cancel_after_recovery(
            request: AuditRoundRequest,
            progress: AuditRoundProgress,
            round_num: int,
            error: InvocationFailureError,
        ) -> bool:
            nonlocal recovery_completed
            assert error is failure
            recovered = recover(request, progress, round_num, error)
            assert recovered
            recovery_completed = True
            loop.call_soon_threadsafe(task.cancel, "first cancellation")
            loop.call_soon_threadsafe(task.cancel, "second cancellation")
            return recovered

        monkeypatch.setattr(
            audit_round,
            "recover_after_remediation_context_exhaustion",
            cancel_after_recovery,
        )
        fatal = scenario in {"fatal", "fatal_before_depth_limit"}
        expected = orchestration.ReviewPolicyError if fatal else asyncio.CancelledError
        with closing(runtime.runtime_publications):
            task = asyncio.create_task(
                execute_sequential_stage(
                    runtime.plan.nodes[0], output, runtime, invoker
                )
            )
            with pytest.raises(expected):
                await asyncio.wait_for(task, timeout=REVIEW_STAGE_TIMEOUT_SECONDS)
            assert recovery_completed
            assert [(call["role"], call["round_num"]) for call in invoker.calls] == [
                ("executor", 1),
                ("reviewer", 1),
                ("executor", 2),
            ]
            marker = output.read_review_checkpoint("review")
            if fatal:
                assert isinstance(marker, ClosedReviewCheckpoint)
                assert "failed to reach consensus" in marker.terminal_reason
            else:
                assert isinstance(marker, OpenReviewCheckpoint)
                assert (marker.next_phase, marker.local_round) == ("executors", 2)
            status = json.loads(
                (
                    output.stages_dir / "review/review-state/review-loop-status.json"
                ).read_text()
            )
            assert status["stop_reason"] == (
                "consensus_exhausted" if fatal else "cancelled"
            )
            frontier = validate_resume_frontier(
                write_manifest(output, runtime, "failed" if fatal else "cancelled"),
                runtime.plan,
                observation(runtime, output),
            )
            assert frontier.checkpoint_node_ids == (() if fatal else ("review",))
            assert frontier.closed_checkpoint_ids == (
                frozenset({"review"}) if fatal else frozenset()
            )

    asyncio.run(run())


@pytest.mark.parametrize(
    "boundary",
    [
        "persist_round_review_inbox",
        "emit_review_stall_warning_if_needed",
        "review_phase_reached_consensus",
    ],
)
@pytest.mark.parametrize(
    "scenario",
    [
        "fatal",
        "exhaustion_continue",
        "continue_on_failure",
        "earlier_round",
        "earlier_audit",
        "approved",
    ],
)
def test_cancellation_after_completed_review_preserves_terminal_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    scenario: str,
) -> None:
    async def run() -> None:
        output, runtime = checkpoint_run(
            tmp_path,
            "continue" if scenario == "exhaustion_continue" else "fatal",
            audit_rounds=2 if scenario == "earlier_audit" else 1,
            depth=2 if scenario == "earlier_round" else 1,
            continue_on_failure=scenario == "continue_on_failure",
        )
        blocked = review_output(major="- Fix the bug.", verdict="CHANGES_REQUESTED")
        final_review = review_output() if scenario == "approved" else blocked
        invoker = MockAgentInvoker(
            ["candidate one", blocked, "candidate two", final_review]
        )
        original = getattr(audit_round, boundary)
        loop = asyncio.get_running_loop()
        cancel_requested = False

        def cancel_after_review(*args: object) -> object:
            nonlocal cancel_requested
            result = original(*args)
            if args[-1] == 2:
                cancel_requested = True
                loop.call_soon_threadsafe(task.cancel, "completed review publication")
            return result

        monkeypatch.setattr(audit_round, boundary, cancel_after_review)
        fatal = scenario == "fatal"
        error = orchestration.ReviewPolicyError if fatal else asyncio.CancelledError
        with closing(runtime.runtime_publications):
            task = asyncio.create_task(
                execute_sequential_stage(
                    runtime.plan.nodes[0], output, runtime, invoker
                )
            )
            with pytest.raises(error):
                await asyncio.wait_for(task, timeout=REVIEW_STAGE_TIMEOUT_SECONDS)
            assert cancel_requested
            assert [(call["role"], call["round_num"]) for call in invoker.calls] == [
                ("executor", 1),
                ("reviewer", 1),
                ("executor", 2),
                ("reviewer", 2),
            ]
            marker = output.read_review_checkpoint("review")
            if fatal:
                assert isinstance(marker, ClosedReviewCheckpoint)
                assert "failed to reach consensus" in marker.terminal_reason
            else:
                assert isinstance(marker, OpenReviewCheckpoint)
                assert (marker.next_phase, marker.local_round) == ("reviewers", 2)
            status = json.loads(
                (
                    output.stages_dir / "review/review-state/review-loop-status.json"
                ).read_text()
            )
            assert status["stop_reason"] == (
                "consensus_exhausted" if fatal else "cancelled"
            )
            frontier = validate_resume_frontier(
                write_manifest(output, runtime, "failed" if fatal else "cancelled"),
                runtime.plan,
                observation(runtime, output),
            )
            assert frontier.checkpoint_node_ids == (() if fatal else ("review",))
            assert frontier.closed_checkpoint_ids == (
                frozenset({"review"}) if fatal else frozenset()
            )

    asyncio.run(run())


@pytest.mark.parametrize("cancel_during_publication", [False, True])
@pytest.mark.parametrize("publication_failure", [False, True])
def test_review_failure_survives_status_publication_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_during_publication: bool,
    publication_failure: bool,
) -> None:
    async def run() -> None:
        output, runtime = checkpoint_run(tmp_path, audit_rounds=1, depth=1)
        failure = provider_failure("reviewer process failed")
        storage_error = OSError("status storage failed")
        reviewer_failed = False
        failure_published = False

        class FailingReviewer(MockAgentInvoker):
            async def invoke(self, *args: Any, **kwargs: Any) -> None:
                nonlocal reviewer_failed
                await super().invoke(*args, **kwargs)
                if self.calls[-1]["role"] == "reviewer":
                    reviewer_failed = True
                    raise failure

        publish = orchestration.persist_review_loop_status
        loop = asyncio.get_running_loop()

        def publish_failure(node_dir: Path, payload: ReviewLoopStatusPayload) -> Path:
            nonlocal failure_published
            path = publish(node_dir, payload)
            if reviewer_failed and not failure_published:
                failure_published = True
                if cancel_during_publication:
                    loop.call_soon_threadsafe(task.cancel, "first cancellation")
                    loop.call_soon_threadsafe(task.cancel, "second cancellation")
                if publication_failure:
                    raise storage_error
            return path

        monkeypatch.setattr(
            orchestration, "persist_review_loop_status", publish_failure
        )
        expected = storage_error if publication_failure else failure
        with closing(runtime.runtime_publications):
            task = asyncio.create_task(
                execute_sequential_stage(
                    runtime.plan.nodes[0],
                    output,
                    runtime,
                    FailingReviewer(["candidate", "partial reviewer output"]),
                )
            )
            with pytest.raises(type(expected)) as caught:
                await asyncio.wait_for(task, timeout=REVIEW_STAGE_TIMEOUT_SECONDS)
            assert caught.value is expected
            assert failure_published
            status = json.loads(
                (
                    output.stages_dir / "review/review-state/review-loop-status.json"
                ).read_text()
            )
            assert status["stop_reason"] == "failed"
            marker = open_checkpoint(output)
            assert (marker.next_phase, marker.local_round) == ("reviewers", 1)

    asyncio.run(run())


@pytest.mark.parametrize(
    ("cancel_round", "continue_on_failure"),
    [(2, False), (3, False), (3, True)],
)
def test_cancellation_during_no_progress_decision_preserves_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_round: int,
    continue_on_failure: bool,
) -> None:
    async def run() -> None:
        output, runtime = checkpoint_run(
            tmp_path,
            "continue",
            audit_rounds=1,
            depth=2,
            continue_on_failure=continue_on_failure,
        )
        blocked = review_output(major="- Fix the bug.", verdict="CHANGES_REQUESTED")
        invoker = MockAgentInvoker(["candidate", blocked, "candidate", "candidate"])
        warn = audit_round.emit_no_progress_warning
        loop = asyncio.get_running_loop()
        cancel_requested = False

        def cancel_during_warning(
            telemetry: ExecutionTelemetry | None,
            node_id: str,
            audit_round_num: int | None,
            round_num: int,
        ) -> None:
            nonlocal cancel_requested
            warn(telemetry, node_id, audit_round_num, round_num)
            if round_num == cancel_round:
                cancel_requested = True
                loop.call_soon_threadsafe(task.cancel, "no-progress decision")

        monkeypatch.setattr(
            audit_round, "emit_no_progress_warning", cancel_during_warning
        )
        fatal = cancel_round == 3 and not continue_on_failure
        error = orchestration.ReviewPolicyError if fatal else asyncio.CancelledError
        with closing(runtime.runtime_publications):
            task = asyncio.create_task(
                execute_sequential_stage(
                    runtime.plan.nodes[0], output, runtime, invoker
                )
            )
            with pytest.raises(error):
                await asyncio.wait_for(task, timeout=REVIEW_STAGE_TIMEOUT_SECONDS)
            assert cancel_requested
            assert [(call["role"], call["round_num"]) for call in invoker.calls] == [
                ("executor", 1),
                ("reviewer", 1),
                *[("executor", number) for number in range(2, cancel_round + 1)],
            ]
            marker = output.read_review_checkpoint("review")
            if fatal:
                assert isinstance(marker, ClosedReviewCheckpoint)
                assert "no_progress" in marker.terminal_reason
            else:
                assert isinstance(marker, OpenReviewCheckpoint)
                assert (marker.next_phase, marker.local_round) == (
                    "executors",
                    cancel_round,
                )
            status_path = (
                output.stages_dir / "review/review-state/review-loop-status.json"
            )
            status = json.loads(status_path.read_text())
            assert status["stop_reason"] == ("no_progress" if fatal else "cancelled")
            assert status["consecutive_no_progress_round_count"] == cancel_round - 1
            frontier = validate_resume_frontier(
                write_manifest(output, runtime, "failed" if fatal else "cancelled"),
                runtime.plan,
                observation(runtime, output),
            )
            assert frontier.checkpoint_node_ids == (() if fatal else ("review",))
            assert frontier.closed_checkpoint_ids == (
                frozenset({"review"}) if fatal else frozenset()
            )

    asyncio.run(run())


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
