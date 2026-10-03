from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from crewplane.artifacts.naming import node_state_relative_path
from crewplane.core.review_checkpoint import (
    ClosedReviewCheckpoint,
    OpenReviewCheckpoint,
)
from crewplane.runtime.execution.review_loop.checkpoint_progress import (
    ProgressRestorer,
    encode_progress,
)
from crewplane.runtime.execution.sequential import execute_sequential_stage
from crewplane.runtime.execution.workflow.node import execute_node
from tests.helpers.review_checkpoints import (
    checkpoint_run,
    hydrate_checkpoint,
    open_checkpoint,
)
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    MockAgentInvoker,
    provider_failure,
    review_output,
)


class Interrupted(Exception):
    pass


OUTPUTS = [
    "candidate one",
    review_output(major="- Fix the first issue", verdict="CHANGES_REQUESTED"),
    "candidate two",
    review_output(major="- Fix the second issue", verdict="CHANGES_REQUESTED"),
    "candidate three",
    review_output(verdict="NO_FINDINGS"),
    review_output(verdict="NO_FINDINGS"),
]


@pytest.mark.parametrize(
    "boundary",
    [
        (1, 1, "reviewers"),
        (1, 2, "executors"),
        (1, 2, "reviewers"),
        (1, 3, "executors"),
        (1, 3, "reviewers"),
        (2, 1, "reviewers"),
        (2, 1, "finalize"),
    ],
)
def test_every_committed_boundary_restores_exact_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: tuple[int, int, str]
) -> None:
    asyncio.run(_compare_resume(tmp_path, monkeypatch, boundary))


async def _compare_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: tuple[int, int, str],
    outputs: list[str] = OUTPUTS,
    node_fields: dict[str, object] | None = None,
    invoker_type: type[MockAgentInvoker] = MockAgentInvoker,
) -> None:
    baseline, baseline_runtime = checkpoint_run(
        tmp_path / "baseline", **(node_fields or {})
    )
    baseline_invoker = invoker_type(outputs)
    await execute_node(
        baseline_runtime.plan.nodes[0],
        baseline,
        baseline_invoker,
        baseline_runtime,
        None,
        "checkpoint.task.md",
    )
    expected = open_checkpoint(baseline)
    output, runtime = checkpoint_run(tmp_path / "resumed", **(node_fields or {}))
    invoke_before = invoker_type(outputs)
    publish = output.write_review_checkpoint

    def interrupt_after_publication(record):
        path = publish(record)
        if (
            isinstance(record, OpenReviewCheckpoint)
            and (record.audit, record.local_round, record.next_phase) == boundary
        ):
            raise Interrupted("committed checkpoint")
        return path

    monkeypatch.setattr(output, "write_review_checkpoint", interrupt_after_publication)
    with pytest.raises(Interrupted):
        await execute_node(
            runtime.plan.nodes[0],
            output,
            invoke_before,
            runtime,
            None,
            "checkpoint.task.md",
        )
    source = open_checkpoint(output)
    restored_progress = ProgressRestorer(
        source, output.stages_dir, runtime.plan.nodes[0].provider_records, 3
    ).restore()
    assert encode_progress(output.stages_dir, restored_progress) == source.progress
    if restored_progress.active_audit is not None:
        assert restored_progress.active_audit.stall is restored_progress.stall
    fresh, restored = hydrate_checkpoint(output, runtime)
    invoke_after = invoker_type(outputs[len(invoke_before.calls) :])
    await execute_node(
        restored.plan.nodes[0],
        fresh,
        invoke_after,
        restored,
        None,
        "checkpoint.task.md",
    )
    actual = open_checkpoint(fresh)
    assert len(invoke_before.calls) + len(invoke_after.calls) == len(
        baseline_invoker.calls
    )
    assert [
        (c["role"], c["audit_round_num"], c["round_num"])
        for c in invoke_before.calls + invoke_after.calls
    ] == [
        (c["role"], c["audit_round_num"], c["round_num"])
        for c in baseline_invoker.calls
    ]
    assert actual.progress == expected.progress
    assert (
        actual.resume_origin is not None
        and actual.resume_origin.source_run_id == output.run_id
    )
    state = json.loads(
        (fresh.stages_dir / node_state_relative_path("review")).read_text()
    )
    assert state["resume_origin"]["source_run_id"] == output.run_id


@pytest.mark.parametrize(
    "scenario",
    [
        "invalid_initial",
        "cross_audit_stall",
        "unstructured",
        "repaired",
        "context_exhaustion",
        "consensus_exhaustion",
        "consensus_exhaustion_setting",
    ],
)
def test_resume_preserves_review_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    approved = review_output(verdict="NO_FINDINGS")
    changes = review_output(major="- Fix the issue", verdict="CHANGES_REQUESTED")
    boundary = (1, 2, "executors")
    node_fields = {}
    invoker_type = MockAgentInvoker
    match scenario:
        case "invalid_initial":
            outputs = ["", "valid candidate", approved]
            boundary = (2, 1, "executors")
        case "cross_audit_stall":
            outputs = ["candidate", changes, "candidate", changes, "candidate"]
            node_fields = {"depth": 1, "continue_on_failure": True}
            boundary = (2, 1, "reviewers")
        case "unstructured":
            outputs = [
                "candidate",
                "Consider the unchecked boundary; it still fails for empty input.",
                "corrected candidate",
                approved,
                approved,
            ]
        case "repaired":
            outputs = [
                "candidate",
                review_output(major="- Fix the issue", verdict="NO_FINDINGS"),
                "corrected candidate",
                approved,
                approved,
            ]
        case "context_exhaustion":
            outputs = ["candidate", changes, "context failure", approved]
            boundary = (2, 1, "reviewers")
            invoker_type = ContextExhaustionInvoker
        case "consensus_exhaustion":
            outputs = ["candidate", changes, "changed candidate", changes]
            node_fields = {"depth": 1, "audit_rounds": 1, "continue_on_failure": True}
        case "consensus_exhaustion_setting":
            outputs = ["candidate", changes, "changed candidate", changes]
            node_fields = {
                "depth": 1,
                "audit_rounds": 1,
                "consensus_on_exhaustion": "continue",
            }
            boundary = (1, 2, "finalize")
    asyncio.run(
        _compare_resume(
            tmp_path, monkeypatch, boundary, outputs, node_fields, invoker_type
        )
    )


class ContextExhaustionInvoker(MockAgentInvoker):
    async def invoke(self, *args, **kwargs):
        await super().invoke(*args, **kwargs)
        if self.outputs[len(self.calls) - 1] == "context failure":
            from crewplane.architecture.contracts.invocation_failures import (
                InvocationFailureSummary,
            )
            from crewplane.runtime.agent.failures import InvocationFailureError

            raise InvocationFailureError(
                "session exhausted",
                InvocationFailureSummary(
                    kind="provider_session_context_exhausted",
                    phase="provider_session",
                    source="none",
                    message="session exhausted",
                    advice="retry fresh",
                    condensed=False,
                ),
                None,
            )


def test_round_zero_is_hydrated_as_executor_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run():
        output, runtime = checkpoint_run(tmp_path, review_starts_with="reviewer")
        publish = output.write_review_checkpoint

        def interrupt(record):
            path = publish(record)
            raise Interrupted(str(path))

        monkeypatch.setattr(output, "write_review_checkpoint", interrupt)
        first = MockAgentInvoker(
            [review_output(major="- Existing issue", verdict="CHANGES_REQUESTED")]
        )
        with pytest.raises(Interrupted):
            await execute_sequential_stage(
                runtime.plan.nodes[0], output, runtime, first
            )
        source = open_checkpoint(output)
        assert source.progress.initial_review_completed
        assert source.progress.selected_round_num == 0
        fresh, restored = hydrate_checkpoint(output, runtime)
        second = MockAgentInvoker(["candidate", review_output(verdict="NO_FINDINGS")])
        await execute_node(
            restored.plan.nodes[0], fresh, second, restored, None, "checkpoint.task.md"
        )
        assert [call["round_num"] for call in second.calls] == [1, 1]
        assert "Existing issue" in second.calls[0]["prompt"]
        assert open_checkpoint(fresh).progress.selected_round_num == 1

    asyncio.run(run())


@pytest.mark.parametrize("failure_point", ["materialization", "cleanup", "success"])
def test_finalization_failure_retries_without_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_point: str
) -> None:
    async def run():
        output, runtime = checkpoint_run(tmp_path)

        def fail(*args, **kwargs):
            del args, kwargs
            raise Interrupted(failure_point)

        async def cancel(*args, **kwargs):
            del args, kwargs
            raise asyncio.CancelledError()

        if failure_point == "materialization":
            monkeypatch.setattr(output, "finalize_node", fail)
        elif failure_point == "success":
            monkeypatch.setattr(output, "write_node_success_state", fail)
        else:
            monkeypatch.setattr(
                runtime.generated_file_workspaces,
                "cleanup_node_best_effort_async",
                cancel,
            )
        invoker = MockAgentInvoker(["candidate", review_output(verdict="NO_FINDINGS")])
        with pytest.raises((Interrupted, asyncio.CancelledError)):
            await execute_node(
                runtime.plan.nodes[0],
                output,
                invoker,
                runtime,
                None,
                "checkpoint.task.md",
            )
        assert open_checkpoint(output).next_phase == "finalize"
        fresh, restored = hydrate_checkpoint(output, runtime)
        second = MockAgentInvoker([])
        await execute_node(
            restored.plan.nodes[0], fresh, second, restored, None, "checkpoint.task.md"
        )
        assert second.calls == []
        assert (fresh.stages_dir / node_state_relative_path("review")).is_file()

    asyncio.run(run())


def test_deterministic_findings_failure_closes_checkpoint(tmp_path: Path) -> None:
    async def run():
        output, runtime = checkpoint_run(tmp_path, findings=True)
        invoker = MockAgentInvoker(
            ["candidate without findings", review_output(verdict="NO_FINDINGS")]
        )
        with pytest.raises(RuntimeError, match="findings"):
            await execute_node(
                runtime.plan.nodes[0],
                output,
                invoker,
                runtime,
                None,
                "checkpoint.task.md",
            )
        assert isinstance(
            output.read_review_checkpoint("review"), ClosedReviewCheckpoint
        )

    asyncio.run(run())


def test_failed_second_executor_repeats_entire_phase(tmp_path: Path) -> None:
    class FailingSecondExecutor(MockAgentInvoker):
        async def invoke(self, *args, **kwargs):
            await super().invoke(*args, **kwargs)
            context = kwargs["invocation_context"]
            if (
                context.role == "executor"
                and context.round_num == 2
                and context.task_id.endswith("_1")
            ):
                raise provider_failure("second executor failed")

    async def run():
        providers = [{"provider": "exec", "role": "executor"}] * 2 + [
            {"provider": "review", "role": "reviewer"}
        ]
        output, runtime = checkpoint_run(tmp_path, providers=providers)
        first = FailingSecondExecutor(
            [
                "first candidate a",
                "first candidate b",
                OUTPUTS[1],
                "changed candidate a",
                "changed candidate b",
            ]
        )
        with pytest.raises(RuntimeError, match="failed"):
            await execute_sequential_stage(
                runtime.plan.nodes[0], output, runtime, first
            )
        checkpoint = open_checkpoint(output)
        assert (checkpoint.local_round, checkpoint.next_phase) == (2, "executors")
        fresh, restored = hydrate_checkpoint(output, runtime)
        second = MockAgentInvoker(
            [
                "changed candidate a",
                "changed candidate b",
                review_output(verdict="NO_FINDINGS"),
                review_output(verdict="NO_FINDINGS"),
            ]
        )
        await execute_node(
            restored.plan.nodes[0], fresh, second, restored, None, "checkpoint.task.md"
        )
        assert [(call["task_id"], call["round_num"]) for call in second.calls[:2]] == [
            ("exec_executor_0", 2),
            ("exec_executor_1", 2),
        ]
        assert len(second.calls) == 4

    asyncio.run(run())


@pytest.mark.parametrize("cancelled", [False, True])
def test_incomplete_reviewer_batch_repeats_in_full(
    tmp_path: Path, cancelled: bool
) -> None:
    class InterruptedBatch(MockAgentInvoker):
        async def invoke(self, *args, **kwargs):
            await super().invoke(*args, **kwargs)
            context = kwargs["invocation_context"]
            if context.role == "reviewer" and context.task_id.endswith("_1"):
                if cancelled:
                    raise asyncio.CancelledError()
                raise provider_failure("reviewer failed")

    async def run():
        providers = [{"provider": "exec", "role": "executor"}] + [
            {"provider": "review", "role": "reviewer"}
        ] * 2
        output, runtime = checkpoint_run(tmp_path, providers=providers)
        first = InterruptedBatch(
            ["candidate", review_output(verdict="NO_FINDINGS"), "partial review"]
        )
        with pytest.raises((RuntimeError, asyncio.CancelledError)):
            await execute_sequential_stage(
                runtime.plan.nodes[0], output, runtime, first
            )
        checkpoint = open_checkpoint(output)
        assert checkpoint.next_phase == "reviewers"
        assert not checkpoint.progress.reviews()
        fresh, restored = hydrate_checkpoint(output, runtime)
        second = MockAgentInvoker([review_output(verdict="NO_FINDINGS")] * 2)
        await execute_node(
            restored.plan.nodes[0], fresh, second, restored, None, "checkpoint.task.md"
        )
        assert sorted(call["task_id"] for call in second.calls) == [
            "review_reviewer_0",
            "review_reviewer_1",
        ]

    asyncio.run(run())


def test_settled_reviewer_failure_is_separate_from_feedback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailedReview(MockAgentInvoker):
        async def invoke(self, *args, **kwargs):
            await super().invoke(*args, **kwargs)
            if kwargs["invocation_context"].role == "reviewer":
                raise provider_failure("private failure text should not be feedback")

    async def run():
        output, runtime = checkpoint_run(tmp_path, continue_on_failure=True)
        publish = output.write_review_checkpoint

        def interrupt(record):
            path = publish(record)
            if (
                isinstance(record, OpenReviewCheckpoint)
                and record.next_phase == "executors"
            ):
                raise Interrupted("settled batch")
            return path

        monkeypatch.setattr(output, "write_review_checkpoint", interrupt)
        with pytest.raises(Interrupted):
            await execute_sequential_stage(
                runtime.plan.nodes[0],
                output,
                runtime,
                FailedReview(["candidate", "partial review"]),
            )
        marker = open_checkpoint(output)
        assert len(marker.progress.active_audit.reviewer_failures) == 1
        assert marker.progress.active_audit.latest_reviewer_outputs == []
        fresh, restored = hydrate_checkpoint(output, runtime)
        second = MockAgentInvoker(
            [
                "changed candidate",
                review_output(verdict="NO_FINDINGS"),
                review_output(verdict="NO_FINDINGS"),
            ]
        )
        await execute_node(
            restored.plan.nodes[0], fresh, second, restored, None, "checkpoint.task.md"
        )
        assert "private failure text" not in second.calls[0]["prompt"]
        assert second.calls[0]["round_num"] == 2

    asyncio.run(run())


def test_invalid_remediation_keeps_boundary_observation_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class MutatingInvalidCandidate(MockAgentInvoker):
        async def invoke(self, *args, **kwargs):
            await super().invoke(*args, **kwargs)
            context = kwargs["invocation_context"]
            if context.role == "executor" and context.round_num == 2:
                (tmp_path / "project-state.txt").write_text(
                    "rejected attempt changed project"
                )

    async def run():
        output, runtime = checkpoint_run(tmp_path)
        publish = output.write_review_checkpoint

        def interrupt(record):
            path = publish(record)
            if isinstance(record, OpenReviewCheckpoint) and (
                record.local_round,
                record.next_phase,
            ) == (3, "executors"):
                raise Interrupted("consumed invalid attempt")
            return path

        monkeypatch.setattr(output, "write_review_checkpoint", interrupt)
        with pytest.raises(Interrupted):
            await execute_sequential_stage(
                runtime.plan.nodes[0],
                output,
                runtime,
                MutatingInvalidCandidate(["candidate", OUTPUTS[1], ""]),
            )
        marker = open_checkpoint(output)
        assert marker.progress.active_audit.invalid_candidate_round_count == 1
        assert (
            marker.project_observation.fingerprint
            != marker.progress.active_audit.latest_valid_executor_outputs[
                0
            ].identity.source_fingerprint
        )
        fresh, restored = hydrate_checkpoint(output, runtime)
        second = MockAgentInvoker(
            [
                "repaired candidate",
                review_output(verdict="NO_FINDINGS"),
                review_output(verdict="NO_FINDINGS"),
            ]
        )
        await execute_node(
            restored.plan.nodes[0], fresh, second, restored, None, "checkpoint.task.md"
        )
        assert second.calls[0]["round_num"] == 3
        assert open_checkpoint(fresh).progress.invalid_candidate_round_count == 1

    asyncio.run(run())


def test_concurrent_checkpoint_publications_are_attributed_and_observation_is_uncertain(
    tmp_path: Path,
) -> None:
    from crewplane.artifacts.naming import review_checkpoint_relative_path
    from crewplane.artifacts.resume.validation import validate_resume_frontier
    from crewplane.runtime.execution.activity.telemetry import (
        ExecutionTelemetry,
        RuntimeActivityTracker,
    )
    from tests.helpers.review_checkpoints import observation, write_manifest

    async def run():
        output, runtime = checkpoint_run(tmp_path)
        first = runtime.plan.nodes[0]
        second = first.model_copy(
            update={
                "id": "peer",
                "render_plan_id": "peer",
                "artifact_contract": first.artifact_contract.model_copy(
                    update={
                        "stage_path": "peer",
                        "output_path": "peer-result.md",
                        "result_path": "peer-result.md",
                        "log_path": "peer/logs",
                    }
                ),
            }
        )
        runtime.plan = runtime.plan.model_copy(
            update={
                "nodes": [first, second],
                "execution_order": ["review", "peer"],
                "render_plans": [
                    *runtime.plan.render_plans,
                    runtime.plan.render_plans[0].model_copy(
                        update={"node_id": "peer", "render_plan_id": "peer"}
                    ),
                ],
            }
        )
        tracker = RuntimeActivityTracker()
        tracker.mark_node_running("review")
        tracker.mark_node_running("peer")
        telemetry = ExecutionTelemetry(
            runtime.plan.workflow_name, output.run_id, activity_tracker=tracker
        )
        slow_started, fast_reviewed = asyncio.Event(), asyncio.Event()

        class ConcurrentInvoker(MockAgentInvoker):
            async def invoke(
                self,
                config,
                model,
                prompt,
                output_file,
                cwd,
                log_file=None,
                invocation_context=None,
            ):
                context = invocation_context
                if context.node_id == "peer" and context.role == "executor":
                    slow_started.set()
                    await fast_reviewed.wait()
                if context.node_id == "review" and context.role == "executor":
                    await slow_started.wait()
                await super().invoke(
                    config,
                    model,
                    prompt,
                    output_file,
                    cwd,
                    log_file,
                    invocation_context,
                )
                output_file.write_text(
                    "candidate"
                    if context.role == "executor"
                    else review_output(verdict="NO_FINDINGS")
                )
                if context.node_id == "review" and context.role == "reviewer":
                    fast_reviewed.set()

        invoker = ConcurrentInvoker()
        async with asyncio.timeout(10):
            await asyncio.gather(
                *(
                    execute_sequential_stage(node, output, runtime, invoker, telemetry)
                    for node in runtime.plan.nodes
                )
            )
        for node in runtime.plan.nodes:
            marker = output.read_review_checkpoint(node.id)
            assert isinstance(marker, OpenReviewCheckpoint)
            assert (
                marker.next_phase == "finalize"
                and not marker.project_observation.reliable
            )
            assert (
                output.stages_dir / review_checkpoint_relative_path(node.id)
                in runtime.runtime_publications.snapshot()[0]
            )
        frontier = validate_resume_frontier(
            write_manifest(output, runtime, "failed"),
            runtime.plan,
            observation(runtime, output),
        )
        assert not frontier.checkpoints

    asyncio.run(run())
