import asyncio
import json
from pathlib import Path

import pytest

from crewplane.artifacts import OutputManager
from crewplane.core.preflight.models import (
    ArtifactContract,
    PreflightExecutionNode,
    PreflightExecutionPlan,
    ProviderRecord,
)
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.agent.failures import (
    FailureKind,
    FailurePhase,
    InvocationFailureError,
    InvocationFailureSummary,
)
from crewplane.runtime.execution import review_loop as review_loop_runtime
from crewplane.runtime.execution.common import CompiledRuntimeContext
from crewplane.runtime.execution.consensus import (
    ParsedReviewResult,
    evaluate_review_output,
    render_review_contract,
)
from crewplane.runtime.execution.review_loop import (
    audit_round as review_loop_audit_round,
)
from crewplane.runtime.execution.review_loop import (
    executor_round as review_loop_executor_round,
)
from crewplane.runtime.execution.review_loop import (
    reviewer_round as review_loop_reviewer_round,
)
from crewplane.runtime.execution.review_loop import (
    rounds as review_loop_rounds,
)
from crewplane.runtime.execution.review_loop.types import (
    AuditRoundRequest,
    ExecutorRoundArtifact,
    ExecutorRoundRequest,
    ExecutorRoundRunResult,
    ReviewerRoundArtifact,
    ReviewerRoundRequest,
    ReviewerRoundRunResult,
)
from crewplane.version import SCHEMA_VERSION
from tests.helpers.workspace_records import workspace_selection_record


def _runtime_context() -> CompiledRuntimeContext:
    return CompiledRuntimeContext(
        plan=PreflightExecutionPlan(
            run_id="run-1",
            run_key_name="run-1",
            project_root=".",
            context_root=".",
            manifest_root=".crewplane",
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


def provider(provider: str, role: ProviderRole, task_id: str) -> ProviderRecord:
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
        render_plan_id="review.node",
        provider_records=[
            provider("exec", ProviderRole.EXECUTOR, "exec_executor_0"),
            provider("review", ProviderRole.REVIEWER, "review_reviewer_0"),
        ],
        artifact_contract=ArtifactContract(output_path="review.node-result.md"),
    )


def _worktree_node() -> PreflightExecutionNode:
    return _node().model_copy(
        update={
            "workspace_policy": workspace_selection_record(
                enabled=True,
                kind="worktree",
                clean_start="strict",
                materialization="worktree_checkout",
            )
        }
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
        provider=provider("exec", ProviderRole.EXECUTOR, "exec_executor_0"),
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
        provider=provider("review", ProviderRole.REVIEWER, "review_reviewer_0"),
        task_id="review_reviewer_0",
        evaluation=evaluate_review_output(output_file.read_text(encoding="utf-8")),
        output_file=output_file,
    )


def _write_lineage_state(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "status": "succeeded",
                "workspace": {"lineage_producer": True},
                "result": {
                    "candidate_commit": "a" * 40,
                    "result_commit": "b" * 40,
                    "candidate_tree": "c" * 40,
                    "result_tree": "d" * 40,
                    "changed_path_count": 1,
                },
                "refs": {"result": "refs/crewplane/result"},
                "bundle": {"path": "workspace-bundles/candidate.bundle"},
            }
        ),
        encoding="utf-8",
    )


def _provider_failure(kind: FailureKind, phase: FailurePhase) -> InvocationFailureError:
    return InvocationFailureError(
        "simulated provider failure",
        InvocationFailureSummary(
            kind=kind,
            phase=phase,
            source="stdout_json",
            message=f"simulated {kind}",
            advice="test advice",
            condensed=False,
        ),
        None,
    )


def test_reviewer_outputs_are_ordered_by_declared_reviewer_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = _node()
    node_dir = output.create_stage_dir(node.id)
    session_ids: list[int] = []
    allowed_paths_by_task: dict[str, set[Path]] = {}

    async def fake_guard(request):
        session_ids.append(id(request.drift_session))
        allowed_paths_by_task[request.task_id] = set(request.allowed_paths)
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
            provider("slow", ProviderRole.REVIEWER, "slow_reviewer_0"),
            provider("fast", ProviderRole.REVIEWER, "fast_reviewer_1"),
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
    reviewer_output_paths = {
        node_dir / "slow_reviewer_0_round1.md",
        node_dir / "fast_reviewer_1_round1.md",
    }
    assert allowed_paths_by_task["slow_reviewer_0"] == reviewer_output_paths
    assert allowed_paths_by_task["fast_reviewer_1"] == reviewer_output_paths


def test_executor_drift_guard_allows_runtime_workspace_state_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = _worktree_node()
    node_dir = output.create_stage_dir(node.id)
    captured_allowed_paths: set[Path] = set()

    async def fake_guard(request):
        captured_allowed_paths.update(request.allowed_paths)
        request.output_file.write_text("candidate", encoding="utf-8")
        return 0

    monkeypatch.setattr(
        review_loop_executor_round, "run_provider_call_with_drift_guard", fake_guard
    )
    request = ExecutorRoundRequest(
        runtime_context=_runtime_context(),
        node=node,
        output=output,
        node_dir=node_dir,
        invoker=object(),
        telemetry=None,
        executors=(provider("exec", ProviderRole.EXECUTOR, "exec_executor_0"),),
        artifact_dir=node_dir,
        executor_prompt="Implement.",
        previous_review_packet=None,
        previous_executor_outputs=None,
        audit_round_num=None,
        round_num=1,
    )

    result = asyncio.run(review_loop_executor_round.run_executor_round(request))

    assert [artifact.task_id for artifact in result.outputs] == ["exec_executor_0"]
    assert (
        node_dir / "workspace-state-review.node-exec_executor_0-round1.json"
        in captured_allowed_paths
    )
    assert (
        node_dir / "workspace-bundles" / "review.node-exec_executor_0-round1.bundle"
        in captured_allowed_paths
    )


def test_executor_drift_guard_does_not_allow_workspace_paths_without_managed_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = _node()
    node_dir = output.create_stage_dir(node.id)
    captured_allowed_paths: set[Path] = set()

    async def fake_guard(request):
        captured_allowed_paths.update(request.allowed_paths)
        request.output_file.write_text("candidate", encoding="utf-8")
        return 0

    monkeypatch.setattr(
        review_loop_executor_round, "run_provider_call_with_drift_guard", fake_guard
    )
    request = ExecutorRoundRequest(
        runtime_context=_runtime_context(),
        node=node,
        output=output,
        node_dir=node_dir,
        invoker=object(),
        telemetry=None,
        executors=(provider("exec", ProviderRole.EXECUTOR, "exec_executor_0"),),
        artifact_dir=node_dir,
        executor_prompt="Implement.",
        previous_review_packet=None,
        previous_executor_outputs=None,
        audit_round_num=None,
        round_num=1,
    )

    asyncio.run(review_loop_executor_round.run_executor_round(request))

    assert node_dir / "exec_executor_0_round1.md" in captured_allowed_paths
    assert (
        node_dir / "workspace-state-review.node-exec_executor_0-round1.json"
        not in captured_allowed_paths
    )
    assert (
        node_dir / "workspace-bundles" / "review.node-exec_executor_0-round1.bundle"
        not in captured_allowed_paths
    )


def test_seed_executor_outputs_aliases_generated_file_workspace_roots(
    tmp_path: Path,
) -> None:
    runtime_context = _runtime_context()
    node_id = "review.node"
    node_dir = tmp_path / "node"
    audit_dir = node_dir / "audit-round-2"
    audit_dir.mkdir(parents=True)
    original_output = node_dir / "exec_executor_0_round2.md"
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    artifact = ExecutorRoundArtifact(
        provider=provider("exec", ProviderRole.EXECUTOR, "exec_executor_0"),
        task_id="exec_executor_0",
        content="Updated `src/app.txt`.",
        output_file=original_output,
    )
    runtime_context.generated_file_workspaces.record(
        node_id,
        original_output,
        workspace_root,
    )

    seeded = review_loop_runtime.seed_executor_outputs(
        runtime_context,
        node_id,
        audit_dir,
        [artifact],
        1,
    )

    seeded_output = audit_dir / "exec_executor_0_round1.md"
    roots = runtime_context.generated_file_workspaces.roots_for_node(node_id)
    assert seeded[0].output_file == seeded_output
    assert roots[seeded_output.resolve(strict=False)] == workspace_root.resolve(
        strict=False
    )


def test_parallel_reviewer_success_is_persisted_before_peer_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = _node()
    node_dir = output.create_stage_dir(node.id)

    async def fake_guard(request):
        if request.provider.provider == "failed":
            raise RuntimeError("simulated reviewer failure")
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
            provider("ok", ProviderRole.REVIEWER, "ok_reviewer_0"),
            provider("failed", ProviderRole.REVIEWER, "failed_reviewer_1"),
        ),
        artifact_dir=node_dir,
        reviewer_prompt_context="Review task.",
        review_context="Candidate",
        previous_review_packet=None,
        audit_round_num=None,
        round_num=1,
    )

    with pytest.raises(RuntimeError, match="simulated reviewer failure"):
        asyncio.run(review_loop_rounds.run_reviewer_round(request))

    assert (node_dir / "ok_reviewer_0_round1.raw.txt").exists()
    assert (node_dir / "ok_reviewer_0_round1.review.json").exists()
    assert (node_dir / "review-state" / "ok-reviewer-0-round-1.state.json").exists()
    failure_state = node_dir / "review-state" / "failed-reviewer-1-round-1.state.json"
    assert failure_state.exists()
    payload = json.loads(failure_state.read_text(encoding="utf-8"))
    assert payload["evaluation_kind"] == "reviewer_failure"
    assert payload["failure_kind"] == "invocation_failed"


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
        executors=(provider("exec", ProviderRole.EXECUTOR, "exec_executor_0"),),
        reviewers=(provider("review", ProviderRole.REVIEWER, "review_reviewer_0"),),
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
        executors=(provider("exec", ProviderRole.EXECUTOR, "exec_executor_0"),),
        reviewers=(provider("review", ProviderRole.REVIEWER, "review_reviewer_0"),),
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


def test_remediation_context_exhaustion_keeps_latest_valid_candidate(
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

    async def fail_executor(request):  # noqa: ARG001 - Test double callback signature.
        raise _provider_failure(
            "provider_session_context_exhausted",
            "provider_session",
        )

    monkeypatch.setattr(review_loop_audit_round, "run_reviewer_round", fake_review)
    monkeypatch.setattr(review_loop_audit_round, "run_executor_round", fail_executor)
    request = AuditRoundRequest(
        runtime_context=_runtime_context(),
        stage=node,
        output=output,
        node_dir=node_dir,
        invoker=object(),
        telemetry=None,
        executors=(provider("exec", ProviderRole.EXECUTOR, "exec_executor_0"),),
        reviewers=(provider("review", ProviderRole.REVIEWER, "review_reviewer_0"),),
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
    assert result.last_round_num == 2
    assert result.latest_executor_outputs is not None
    assert result.latest_executor_outputs[0].content == candidate


def test_remediation_context_exhaustion_discards_recovered_executor_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = _worktree_node()
    node_dir = output.create_stage_dir(node.id)
    candidate = "Candidate body"
    state_paths = [
        node_dir / "workspace-state-review.node-fast_executor_0-round2.json",
        node_dir / "workspace-state-review.node-failed_executor_1-round2.json",
    ]

    async def fake_review(request):  # noqa: ARG001 - Test double callback signature.
        return ReviewerRoundRunResult(
            outputs=[_reviewer_artifact(node_dir, "- Fix the retry branch")],
            drift_warning_count=0,
        )

    async def fail_executor(request):  # noqa: ARG001 - Test double callback signature.
        for state_path in state_paths:
            _write_lineage_state(state_path)
        raise _provider_failure(
            "provider_session_context_exhausted",
            "provider_session",
        )

    monkeypatch.setattr(review_loop_audit_round, "run_reviewer_round", fake_review)
    monkeypatch.setattr(review_loop_audit_round, "run_executor_round", fail_executor)
    request = AuditRoundRequest(
        runtime_context=_runtime_context(),
        stage=node,
        output=output,
        node_dir=node_dir,
        invoker=object(),
        telemetry=None,
        executors=(
            provider("fast", ProviderRole.EXECUTOR, "fast_executor_0"),
            provider("failed", ProviderRole.EXECUTOR, "failed_executor_1"),
        ),
        reviewers=(provider("review", ProviderRole.REVIEWER, "review_reviewer_0"),),
        executor_prompt="Implement.",
        reviewer_prompt_context="Review.",
        audit_dir=node_dir,
        remediation_depth=1,
        initial_executor_outputs=[_executor_artifact(node_dir, candidate)],
        audit_round_num=None,
    )

    result = asyncio.run(review_loop_rounds.execute_single_audit_round(request))

    assert not result.consensus_reached
    assert result.latest_executor_outputs is not None
    assert result.latest_executor_outputs[0].content == candidate
    for state_path in state_paths:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["workspace"]["lineage_producer"] is False
        assert state["result"]["lineage_produced"] is False
        assert state["result"]["lineage_discarded"] is True
        assert (
            state["result"]["lineage_discard_reason"] == "remediation_context_exhausted"
        )
        assert "refs" not in state
        assert "bundle" not in state


def test_remediation_non_context_invocation_failure_still_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = _node()
    node_dir = output.create_stage_dir(node.id)

    async def fake_review(request):  # noqa: ARG001 - Test double callback signature.
        return ReviewerRoundRunResult(
            outputs=[_reviewer_artifact(node_dir, "- Fix the retry branch")],
            drift_warning_count=0,
        )

    async def fail_executor(request):  # noqa: ARG001 - Test double callback signature.
        raise _provider_failure("quota_or_rate_limit", "provider_transport")

    monkeypatch.setattr(review_loop_audit_round, "run_reviewer_round", fake_review)
    monkeypatch.setattr(review_loop_audit_round, "run_executor_round", fail_executor)
    request = AuditRoundRequest(
        runtime_context=_runtime_context(),
        stage=node,
        output=output,
        node_dir=node_dir,
        invoker=object(),
        telemetry=None,
        executors=(provider("exec", ProviderRole.EXECUTOR, "exec_executor_0"),),
        reviewers=(provider("review", ProviderRole.REVIEWER, "review_reviewer_0"),),
        executor_prompt="Implement.",
        reviewer_prompt_context="Review.",
        audit_dir=node_dir,
        remediation_depth=1,
        initial_executor_outputs=[_executor_artifact(node_dir, "Candidate body")],
        audit_round_num=None,
    )

    with pytest.raises(InvocationFailureError) as caught:
        asyncio.run(review_loop_rounds.execute_single_audit_round(request))

    assert caught.value.kind == "quota_or_rate_limit"


def test_no_progress_candidate_discards_executor_workspace_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = _worktree_node()
    node_dir = output.create_stage_dir(node.id)
    candidate = "Candidate body"
    state_path = node_dir / "workspace-state-review.node-exec_executor_0-round2.json"

    async def fake_review(request):  # noqa: ARG001 - Test double callback signature.
        return ReviewerRoundRunResult(
            outputs=[_reviewer_artifact(node_dir, "- Fix the retry branch")],
            drift_warning_count=0,
        )

    async def fake_executor(request):  # noqa: ARG001 - Test double callback signature.
        _write_lineage_state(state_path)
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
        executors=(provider("exec", ProviderRole.EXECUTOR, "exec_executor_0"),),
        reviewers=(provider("review", ProviderRole.REVIEWER, "review_reviewer_0"),),
        executor_prompt="Implement.",
        reviewer_prompt_context="Review.",
        audit_dir=node_dir,
        remediation_depth=1,
        initial_executor_outputs=[_executor_artifact(node_dir, candidate)],
        audit_round_num=None,
    )

    result = asyncio.run(review_loop_rounds.execute_single_audit_round(request))

    assert not result.consensus_reached
    assert result.no_progress_round_count == 1
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["workspace"]["lineage_producer"] is False
    assert state["result"]["lineage_produced"] is False
    assert state["result"]["lineage_discarded"] is True
    assert state["result"]["lineage_discard_reason"] == "no_progress_candidate"
    assert "refs" not in state
    assert "bundle" not in state
