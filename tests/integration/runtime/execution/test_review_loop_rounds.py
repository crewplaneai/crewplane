import asyncio
from pathlib import Path

import pytest

from orchestrator_cli.artifacts import OutputManager
from orchestrator_cli.core.preflight.models import (
    ArtifactContract,
    PreflightExecutionNode,
    PreflightExecutionPlan,
    ProviderRecord,
)
from orchestrator_cli.core.preflight.secrets import SecretContext
from orchestrator_cli.runtime.execution.common import CompiledRuntimeContext
from orchestrator_cli.runtime.execution.consensus import (
    ParsedReviewResult,
    evaluate_review_output,
    render_review_contract,
)
from orchestrator_cli.runtime.execution.review_loop import (
    audit_round as review_loop_audit_round,
)
from orchestrator_cli.runtime.execution.review_loop import (
    reviewer_round as review_loop_reviewer_round,
)
from orchestrator_cli.runtime.execution.review_loop import (
    rounds as review_loop_rounds,
)
from orchestrator_cli.runtime.execution.review_loop.types import (
    AuditRoundRequest,
    ExecutorRoundArtifact,
    ExecutorRoundRunResult,
    ReviewerRoundArtifact,
    ReviewerRoundRequest,
    ReviewerRoundRunResult,
)
from orchestrator_cli.version import SCHEMA_VERSION


def _runtime_context() -> CompiledRuntimeContext:
    return CompiledRuntimeContext(
        plan=PreflightExecutionPlan(
            run_id="run-1",
            run_key_name="run-1",
            context_root=".",
            manifest_root=".orchestrator",
            created_at="2026-06-03T00:00:00",
            workflow_name="workflow",
            workflow_signature="workflow-signature",
            execution_order=["review.node"],
            nodes=[_node()],
            render_plans=[],
            static_resources=[],
            token_catalog=[],
            dependency_graph=[],
            runtime_config_snapshot={"schema_version": SCHEMA_VERSION},
            effective_runtime_config_signature="runtime-signature",
            fingerprint_metadata={"payload_version": "1"},
        ),
        secret_context=SecretContext(),
    )


def provider(provider: str, role: str, task_id: str) -> ProviderRecord:
    return ProviderRecord(
        provider=provider,
        role=role,
        task_id=task_id,
        agent_config_key=provider,
        invoker_alias="mock",
        agent_config_signature=f"{provider}-agent",
        invoker_config_signature="mock-config",
    )


def _node() -> PreflightExecutionNode:
    return PreflightExecutionNode(
        id="review.node",
        mode="sequential",
        artifact_contract=ArtifactContract(output_path="review.node-result.md"),
    )


def review_output(verdict: str = "NO_FINDINGS", major: str = "None") -> str:
    return render_review_contract(
        ParsedReviewResult(
            verdict=verdict,
            major_issues=major,
            minor_issues="None",
            nitpicks="None",
        )
    )


def _executor_artifact(node_dir: Path, content: str) -> ExecutorRoundArtifact:
    return ExecutorRoundArtifact(
        provider=provider("exec", "executor", "exec_executor_0"),
        task_id="exec_executor_0",
        content=content,
        output_file=node_dir / "exec_executor_0_round1.md",
    )


def _reviewer_artifact(node_dir: Path, major: str) -> ReviewerRoundArtifact:
    output_file = node_dir / "review_reviewer_0_round1.md"
    output_file.write_text(
        review_output(verdict="CHANGES_REQUESTED", major=major),
        encoding="utf-8",
    )
    return ReviewerRoundArtifact(
        provider=provider("review", "reviewer", "review_reviewer_0"),
        task_id="review_reviewer_0",
        evaluation=evaluate_review_output(output_file.read_text(encoding="utf-8")),
        output_file=output_file,
    )


def test_reviewer_outputs_are_ordered_by_declared_reviewer_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = _node()
    node_dir = output.create_stage_dir(node.id)
    session_ids: list[int] = []

    async def fake_guard(request):
        session_ids.append(id(request.drift_session))
        if request.provider.provider == "slow":
            await asyncio.sleep(0.02)
        request.output_file.write_text(review_output(), encoding="utf-8")
        return 0

    monkeypatch.setattr(
        review_loop_reviewer_round, "run_provider_call_with_drift_guard", fake_guard
    )
    request = ReviewerRoundRequest(
        runtime_context=_runtime_context(),
        node=node,
        output=output,
        node_dir=node_dir,
        invoker=object(),
        telemetry=None,
        reviewers=(
            provider("slow", "reviewer", "slow_reviewer_0"),
            provider("fast", "reviewer", "fast_reviewer_1"),
        ),
        artifact_dir=node_dir,
        reviewer_prompt_context="Review task.",
        review_context="Candidate",
        previous_review_packet=None,
        audit_round_num=None,
        round_num=1,
    )

    result = asyncio.run(review_loop_rounds.run_reviewer_round(request))

    assert [artifact.task_id for artifact in result.outputs] == [
        "slow_reviewer_0",
        "fast_reviewer_1",
    ]
    assert len(set(session_ids)) == 1


def test_invalid_candidate_round_skips_reviewers_and_tracks_accounting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = _node()
    node_dir = output.create_stage_dir(node.id)

    async def fail_review(request):  # noqa: ARG001 - Test double callback signature.
        raise AssertionError("reviewer should not run for an invalid candidate")

    monkeypatch.setattr(review_loop_audit_round, "run_reviewer_round", fail_review)
    request = AuditRoundRequest(
        runtime_context=_runtime_context(),
        stage=node,
        output=output,
        node_dir=node_dir,
        invoker=object(),
        telemetry=None,
        executors=(provider("exec", "executor", "exec_executor_0"),),
        reviewers=(provider("review", "reviewer", "review_reviewer_0"),),
        executor_prompt="Implement.",
        reviewer_prompt_context="Review.",
        audit_dir=node_dir,
        remediation_depth=1,
        initial_executor_outputs=[_executor_artifact(node_dir, "   ")],
        audit_round_num=None,
    )

    result = asyncio.run(review_loop_rounds.execute_single_audit_round(request))

    assert not result.consensus_reached
    assert result.invalid_candidate_round_count == 1
    assert result.no_progress_round_count == 0
    assert result.latest_executor_outputs is None


def test_no_progress_candidate_skips_second_review_round_and_tracks_accounting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = _node()
    node_dir = output.create_stage_dir(node.id)
    candidate = "Candidate body"
    review_calls = 0

    async def fake_review(request):  # noqa: ARG001 - Test double callback signature.
        nonlocal review_calls
        review_calls += 1
        return ReviewerRoundRunResult(
            outputs=[_reviewer_artifact(node_dir, "- Fix the retry branch")],
            drift_warning_count=0,
        )

    async def fake_executor(request):  # noqa: ARG001 - Test double callback signature.
        return ExecutorRoundRunResult(
            outputs=[_executor_artifact(node_dir, candidate)],
            drift_warning_count=0,
        )

    monkeypatch.setattr(review_loop_audit_round, "run_reviewer_round", fake_review)
    monkeypatch.setattr(review_loop_audit_round, "run_executor_round", fake_executor)
    request = AuditRoundRequest(
        runtime_context=_runtime_context(),
        stage=node,
        output=output,
        node_dir=node_dir,
        invoker=object(),
        telemetry=None,
        executors=(provider("exec", "executor", "exec_executor_0"),),
        reviewers=(provider("review", "reviewer", "review_reviewer_0"),),
        executor_prompt="Implement.",
        reviewer_prompt_context="Review.",
        audit_dir=node_dir,
        remediation_depth=1,
        initial_executor_outputs=[_executor_artifact(node_dir, candidate)],
        audit_round_num=None,
    )

    result = asyncio.run(review_loop_rounds.execute_single_audit_round(request))

    assert review_calls == 1
    assert not result.consensus_reached
    assert result.no_progress_round_count == 1
    assert result.latest_executor_outputs is not None
    assert result.latest_executor_outputs[0].content == candidate
