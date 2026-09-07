import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from crewplane.artifacts import OutputManager
from crewplane.core.file_hashing import file_size_and_sha256
from crewplane.core.preflight.models import (
    ArtifactContract,
    ExecutionPolicy,
    PreflightExecutionNode,
    PreflightExecutionPlan,
    ProviderRecord,
    RenderPlan,
)
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.agent.failures import (
    FailureKind,
    FailurePhase,
    InvocationFailureError,
    InvocationFailureSummary,
)
from crewplane.runtime.execution import NodeExecutionError
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
from crewplane.runtime.execution.runtime_context import (
    agent_config_signature_from_plan,
    invoker_config_signature_from_plan,
)
from crewplane.runtime.workspace.setup import WorkspaceSetupError
from crewplane.version import SCHEMA_VERSION
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.workspace_records import workspace_selection_record


def _runtime_context() -> CompiledRuntimeContext:
    return CompiledRuntimeContext(
        plan=PreflightExecutionPlan(
            plan_schema_version=SCHEMA_VERSION,
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
            render_plans=[
                RenderPlan(render_plan_id="review.node", node_id="review.node")
            ],
            static_resources=[],
            token_catalog=[],
            dependency_graph=[],
            runtime_config_snapshot={
                "execution": {"sequential_consensus_on_exhaustion": "continue"},
                "schema_version": SCHEMA_VERSION,
            },
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
        execution_policy=ExecutionPolicy(consensus_on_exhaustion="continue"),
        provider_records=[
            provider("exec", ProviderRole.EXECUTOR, "exec_executor_0"),
            provider("review", ProviderRole.REVIEWER, "review_reviewer_0"),
        ],
        artifact_contract=ArtifactContract(
            stage_path="review.node",
            output_path="review.node-result.md",
            log_path="review.node/logs",
            result_path="review.node-result.md",
        ),
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
        audit_round_num=None,
        round_num=1,
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
        audit_round_num=None,
        round_num=1,
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
    node_dir = output.create_node_dir(node_artifact_request(node.id))
    session_ids: list[int] = []
    allowed_paths_by_task: dict[str, set[Path]] = {}
    fast_finished = asyncio.Event()
    completion_order: list[str] = []

    async def fake_guard(request):
        session_ids.append(id(request.drift_session))
        allowed_paths_by_task[request.task_id] = set(request.allowed_paths)
        if request.provider.provider == "slow":
            await asyncio.wait_for(fast_finished.wait(), timeout=1.0)
        assert request.invocation_output_file is not None
        request.invocation_output_file.write_text(review_output(), encoding="utf-8")
        completion_order.append(request.provider.provider)
        if request.provider.provider == "fast":
            fast_finished.set()
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

    assert completion_order == ["fast", "slow"]
    assert [artifact.task_id for artifact in result.outputs] == [
        "slow_reviewer_0",
        "fast_reviewer_1",
    ]
    assert len(set(session_ids)) == 2
    assert allowed_paths_by_task["slow_reviewer_0"] == set()
    assert allowed_paths_by_task["fast_reviewer_1"] == set()


def test_parallel_reviewer_sessions_share_recovery_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = _node()
    node_dir = output.create_node_dir(node_artifact_request(node.id))
    result_path = output.results_dir / "prior-result.md"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_bytes(b"prior result")
    recovery_baselines: list[object] = []

    async def fake_guard(request):
        assert request.drift_session is not None
        recovery_baselines.append(request.drift_session.recovery_baseline)
        assert request.invocation_output_file is not None
        request.invocation_output_file.write_text(review_output(), encoding="utf-8")
        return 0

    monkeypatch.setattr(
        review_loop_reviewer_round,
        "run_provider_call_with_drift_guard",
        fake_guard,
    )
    request = ReviewerRoundRequest(
        runtime_context=_runtime_context(),
        node=node,
        output=output,
        node_dir=node_dir,
        invoker=object(),
        telemetry=None,
        reviewers=(
            provider("first", ProviderRole.REVIEWER, "first_reviewer_0"),
            provider("second", ProviderRole.REVIEWER, "second_reviewer_1"),
        ),
        artifact_dir=node_dir,
        reviewer_prompt_context="Review task.",
        review_context="Candidate",
        previous_review_packet=None,
        audit_round_num=None,
        round_num=1,
    )

    asyncio.run(review_loop_rounds.run_reviewer_round(request))

    assert recovery_baselines[0] is not None
    assert recovery_baselines[0] is recovery_baselines[1]
    request.runtime_context.runtime_publications.close()


def test_parallel_reviewers_publish_only_after_every_invocation_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = _node()
    node_dir = output.create_node_dir(node_artifact_request(node.id))
    fast_finished = asyncio.Event()
    release_slow = asyncio.Event()
    inspect_fast_publication: Callable[[], None] | None = None

    async def fake_guard(request):
        assert request.invocation_output_file is not None
        request.invocation_output_file.write_text(review_output(), encoding="utf-8")
        if request.provider.provider == "fast":
            fast_finished.set()
            return 0
        await asyncio.wait_for(fast_finished.wait(), timeout=1.0)
        assert inspect_fast_publication is not None
        inspect_fast_publication()
        await asyncio.wait_for(release_slow.wait(), timeout=1.0)
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
    fast_output = node_dir / "fast_reviewer_1_round1.md"

    async def run_and_release() -> ReviewerRoundRunResult:
        nonlocal inspect_fast_publication

        def assert_fast_output_is_private() -> None:
            assert not fast_output.exists()

        inspect_fast_publication = assert_fast_output_is_private
        round_task = asyncio.create_task(review_loop_rounds.run_reviewer_round(request))
        await asyncio.wait_for(fast_finished.wait(), timeout=1.0)
        await asyncio.sleep(0)
        assert not fast_output.exists()
        release_slow.set()
        return await round_task

    result = asyncio.run(run_and_release())

    assert [artifact.task_id for artifact in result.outputs] == [
        "slow_reviewer_0",
        "fast_reviewer_1",
    ]
    assert fast_output.exists()


def test_peer_private_output_mutation_is_rejected_at_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = _node().model_copy(
        update={"execution_policy": ExecutionPolicy(continue_on_failure=True)}
    )
    node_dir = output.create_node_dir(node_artifact_request(node.id))
    blocker_finished = asyncio.Event()
    private_outputs: dict[str, Path] = {}

    async def fake_guard(request):
        assert request.invocation_output_file is not None
        private_outputs[request.task_id] = request.invocation_output_file
        if request.task_id == "blocker_reviewer_0":
            request.invocation_output_file.write_text(
                review_output(
                    verdict="CHANGES_REQUESTED",
                    major="The candidate is unsafe.",
                ),
                encoding="utf-8",
            )
            blocker_finished.set()
            return 0
        await asyncio.wait_for(blocker_finished.wait(), timeout=1.0)
        private_outputs["blocker_reviewer_0"].write_text(
            review_output(),
            encoding="utf-8",
        )
        request.invocation_output_file.write_text(review_output(), encoding="utf-8")
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
            provider("blocker", ProviderRole.REVIEWER, "blocker_reviewer_0"),
            provider("peer", ProviderRole.REVIEWER, "peer_reviewer_1"),
        ),
        artifact_dir=node_dir,
        reviewer_prompt_context="Review task.",
        review_context="Candidate",
        previous_review_packet=None,
        audit_round_num=None,
        round_num=1,
    )

    result = asyncio.run(review_loop_rounds.run_reviewer_round(request))

    assert result.reviewer_failure_count == 1
    assert [artifact.task_id for artifact in result.outputs] == ["peer_reviewer_1"]
    assert not (node_dir / "blocker_reviewer_0_round1.md").exists()


def test_reviewer_verdict_uses_bound_private_output_after_canonical_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = _node()
    node_dir = output.create_node_dir(node_artifact_request(node.id))
    blocking_output = review_output(
        verdict="CHANGES_REQUESTED",
        major="The candidate is unsafe.",
    )

    async def fake_guard(request):
        assert request.invocation_output_file is not None
        request.invocation_output_file.write_text(blocking_output, encoding="utf-8")
        return 0

    original_publish = review_loop_reviewer_round.publish_invocation_output

    def publish_then_mutate_canonical(
        invocation_output_file,
        output_file,
        publications,
        expected_signature=None,
    ):
        signature = original_publish(
            invocation_output_file,
            output_file,
            publications,
            expected_signature,
        )
        output_file.write_text(review_output(), encoding="utf-8")
        return signature

    monkeypatch.setattr(
        review_loop_reviewer_round, "run_provider_call_with_drift_guard", fake_guard
    )
    monkeypatch.setattr(
        review_loop_reviewer_round,
        "publish_invocation_output",
        publish_then_mutate_canonical,
    )
    request = ReviewerRoundRequest(
        runtime_context=_runtime_context(),
        node=node,
        output=output,
        node_dir=node_dir,
        invoker=object(),
        telemetry=None,
        reviewers=(provider("blocker", ProviderRole.REVIEWER, "blocker_reviewer_0"),),
        artifact_dir=node_dir,
        reviewer_prompt_context="Review task.",
        review_context="Candidate",
        previous_review_packet=None,
        audit_round_num=None,
        round_num=1,
    )

    result = asyncio.run(review_loop_rounds.run_reviewer_round(request))

    assert result.outputs[0].evaluation.verdict == "CHANGES_REQUESTED"
    assert result.outputs[0].evaluation.major_issues == "The candidate is unsafe."


def test_reviewer_normalization_rejects_substituted_publication_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = _node().model_copy(
        update={"execution_policy": ExecutionPolicy(continue_on_failure=True)}
    )
    node_dir = output.create_node_dir(node_artifact_request(node.id))

    async def fake_guard(request):
        assert request.invocation_output_file is not None
        request.invocation_output_file.write_text(
            review_output(
                verdict="CHANGES_REQUESTED",
                major="The candidate is unsafe.",
            ),
            encoding="utf-8",
        )
        return 0

    original_publish = review_loop_reviewer_round.publish_invocation_output

    def substitute_normalized_source(
        invocation_output_file,
        output_file,
        publications,
        expected_signature=None,
    ):
        invocation_output_file.write_text(review_output(), encoding="utf-8")
        return original_publish(
            invocation_output_file,
            output_file,
            publications,
            expected_signature,
        )

    monkeypatch.setattr(
        review_loop_reviewer_round, "run_provider_call_with_drift_guard", fake_guard
    )
    monkeypatch.setattr(
        review_loop_reviewer_round,
        "publish_invocation_output",
        substitute_normalized_source,
    )
    request = ReviewerRoundRequest(
        runtime_context=_runtime_context(),
        node=node,
        output=output,
        node_dir=node_dir,
        invoker=object(),
        telemetry=None,
        reviewers=(provider("blocker", ProviderRole.REVIEWER, "blocker_reviewer_0"),),
        artifact_dir=node_dir,
        reviewer_prompt_context="Review task.",
        review_context="Candidate",
        previous_review_packet=None,
        audit_round_num=None,
        round_num=1,
    )

    result = asyncio.run(review_loop_rounds.run_reviewer_round(request))

    assert result.outputs == []
    assert result.reviewer_failure_count == 1
    assert not (node_dir / "blocker_reviewer_0_round1.md").exists()


def test_peer_canonical_precreation_is_fatal_before_round_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = _node()
    node_dir = output.create_node_dir(node_artifact_request(node.id))
    runtime_context = _runtime_context()
    runtime_context.plan.runtime_config_snapshot["agents"] = {
        "exec": {"cli_cmd": ["mock"], "default_model": "m1"},
        "review": {"cli_cmd": ["mock"], "default_model": "m2"},
    }
    runtime_context.plan.runtime_config_snapshot["invoker"] = {
        "capabilities": {},
        "implementation": "mock",
        "options": {},
        "option_scopes": {},
        "resolved_identity": "mock",
    }
    invoker_signature = invoker_config_signature_from_plan(runtime_context.plan)
    blocker_provider = provider(
        "exec",
        ProviderRole.REVIEWER,
        "blocker_reviewer_0",
    ).model_copy(
        update={
            "agent_config_signature": agent_config_signature_from_plan(
                runtime_context.plan,
                "exec",
                None,
            ),
            "invoker_config_signature": invoker_signature,
        }
    )
    attacker_provider = provider(
        "review",
        ProviderRole.REVIEWER,
        "attacker_reviewer_1",
    ).model_copy(
        update={
            "agent_config_signature": agent_config_signature_from_plan(
                runtime_context.plan,
                "review",
                None,
            ),
            "invoker_config_signature": invoker_signature,
        }
    )
    runtime_context.plan = runtime_context.plan.model_copy(
        update={
            "nodes": [
                runtime_context.plan.nodes[0].model_copy(
                    update={
                        "provider_records": [
                            blocker_provider,
                            attacker_provider,
                        ]
                    }
                )
            ]
        }
    )
    blocking_output = review_output(
        verdict="CHANGES_REQUESTED",
        major="The candidate is unsafe.",
    )
    blocker_output_written = asyncio.Event()
    peer_output_written = asyncio.Event()
    blocker_guard_finished = asyncio.Event()
    original_run_with_drift_guard = (
        review_loop_reviewer_round.run_provider_call_with_drift_guard
    )

    async def record_blocker_guard_finished(request):  # type: ignore[no-untyped-def]
        try:
            return await original_run_with_drift_guard(request)
        finally:
            if request.task_id == "blocker_reviewer_0":
                blocker_guard_finished.set()

    monkeypatch.setattr(
        review_loop_reviewer_round,
        "run_provider_call_with_drift_guard",
        record_blocker_guard_finished,
    )

    class PeerPrecreatingInvoker:
        def log_presentation_for(self, config):  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by protocol.
            return None

        async def invoke(  # type: ignore[no-untyped-def]
            self,
            config,  # noqa: ARG002 - Required by protocol.
            model,  # noqa: ARG002 - Required by protocol.
            prompt,  # noqa: ARG002 - Required by protocol.
            output_file,
            cwd,  # noqa: ARG002 - Required by protocol.
            log_file=None,  # noqa: ARG002 - Required by protocol.
            invocation_context=None,
        ) -> None:
            assert invocation_context is not None
            if invocation_context.task_id == "attacker_reviewer_1":
                await asyncio.wait_for(blocker_output_written.wait(), timeout=1.0)
                peer_path = node_dir / "blocker_reviewer_0_round1.md"
                peer_path.write_text(review_output(), encoding="utf-8")
                runtime_context.runtime_publications.publish(
                    peer_path,
                    file_size_and_sha256(peer_path),
                )
                output_file.write_text(review_output(), encoding="utf-8")
                peer_output_written.set()
                await asyncio.wait_for(blocker_guard_finished.wait(), timeout=1.0)
                return
            output_file.write_text(blocking_output, encoding="utf-8")
            blocker_output_written.set()
            await asyncio.wait_for(peer_output_written.wait(), timeout=1.0)

    request = ReviewerRoundRequest(
        runtime_context=runtime_context,
        node=node.model_copy(
            update={"execution_policy": ExecutionPolicy(continue_on_failure=True)}
        ),
        output=output,
        node_dir=node_dir,
        invoker=PeerPrecreatingInvoker(),
        telemetry=None,
        reviewers=(blocker_provider, attacker_provider),
        artifact_dir=node_dir,
        reviewer_prompt_context="Review task.",
        review_context="Candidate",
        previous_review_packet=None,
        audit_round_num=None,
        round_num=1,
    )

    result = asyncio.run(review_loop_rounds.run_reviewer_round(request))

    assert result.reviewer_failure_count == 1
    assert [artifact.task_id for artifact in result.outputs] == ["blocker_reviewer_0"]
    canonical_blocker = node_dir / "blocker_reviewer_0_round1.md"
    assert canonical_blocker.read_text(encoding="utf-8") == blocking_output
    assert result.outputs[0].evaluation.verdict == "CHANGES_REQUESTED"


def test_executor_drift_guard_allows_runtime_workspace_state_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = _worktree_node()
    node_dir = output.create_node_dir(node_artifact_request(node.id))
    captured_allowed_paths: set[Path] = set()

    async def fake_guard(request):
        captured_allowed_paths.update(request.allowed_paths)
        request.output_file.write_text("candidate", encoding="utf-8")
        request.runtime_context.runtime_publications.publish(
            request.output_file,
            file_size_and_sha256(request.output_file),
        )
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
    node_dir = output.create_node_dir(node_artifact_request(node.id))
    captured_allowed_paths: set[Path] = set()

    async def fake_guard(request):
        captured_allowed_paths.update(request.allowed_paths)
        request.output_file.write_text("candidate", encoding="utf-8")
        request.runtime_context.runtime_publications.publish(
            request.output_file,
            file_size_and_sha256(request.output_file),
        )
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


def test_executor_round_rejects_output_changed_after_runtime_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = _node()
    node_dir = output.create_node_dir(node_artifact_request(node.id))
    runtime_context = _runtime_context()

    async def fake_guard(request):
        request.output_file.write_text("trusted candidate", encoding="utf-8")
        runtime_context.runtime_publications.publish(
            request.output_file,
            file_size_and_sha256(request.output_file),
        )
        request.output_file.write_text("substituted candidate", encoding="utf-8")
        return 0

    monkeypatch.setattr(
        review_loop_executor_round, "run_provider_call_with_drift_guard", fake_guard
    )
    request = ExecutorRoundRequest(
        runtime_context=runtime_context,
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

    with pytest.raises(RuntimeError, match="bound bytes"):
        asyncio.run(review_loop_executor_round.run_executor_round(request))


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
    original_output.write_text("Updated `src/app.txt`.", encoding="utf-8")
    artifact = ExecutorRoundArtifact(
        provider=provider("exec", ProviderRole.EXECUTOR, "exec_executor_0"),
        task_id="exec_executor_0",
        content="Updated `src/app.txt`.",
        output_file=original_output,
        audit_round_num=None,
        round_num=2,
        output_signature=file_size_and_sha256(original_output),
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
        2,
        1,
    )

    seeded_output = audit_dir / "exec_executor_0_round1.md"
    roots = runtime_context.generated_file_workspaces.roots_for_node(node_id)
    assert seeded[0].output_file == seeded_output
    assert seeded[0].audit_round_num == 2
    assert seeded[0].round_num == 1
    assert roots[seeded_output.resolve(strict=False)] == workspace_root.resolve(
        strict=False
    )


def test_parallel_reviewer_success_is_persisted_before_peer_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = _node()
    node_dir = output.create_node_dir(node_artifact_request(node.id))

    async def fake_guard(request):
        if request.provider.provider == "failed":
            raise _provider_failure("quota_or_rate_limit", "provider_transport")
        assert request.invocation_output_file is not None
        request.invocation_output_file.write_text(review_output(), encoding="utf-8")
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

    with pytest.raises(InvocationFailureError, match="simulated quota_or_rate_limit"):
        asyncio.run(review_loop_rounds.run_reviewer_round(request))

    assert (node_dir / "ok_reviewer_0_round1.raw.txt").exists()
    assert (node_dir / "ok_reviewer_0_round1.review.json").exists()
    assert (node_dir / "review-state" / "ok-reviewer-0-round-1.state.json").exists()
    failure_state = node_dir / "review-state" / "failed-reviewer-1-round-1.state.json"
    assert failure_state.exists()
    payload = json.loads(failure_state.read_text(encoding="utf-8"))
    assert payload["evaluation_kind"] == "reviewer_failure"
    assert payload["failure_kind"] == "invocation_failed"


def test_parallel_reviewer_defect_propagates_with_continue_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = _node().model_copy(
        update={"execution_policy": ExecutionPolicy(continue_on_failure=True)}
    )
    node_dir = output.create_node_dir(node_artifact_request(node.id))

    async def fake_guard(request):
        if request.provider.provider == "failed":
            raise TypeError("simulated reviewer defect")
        assert request.invocation_output_file is not None
        request.invocation_output_file.write_text(review_output(), encoding="utf-8")
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

    with pytest.raises(TypeError, match="simulated reviewer defect"):
        asyncio.run(review_loop_rounds.run_reviewer_round(request))

    failure_state = node_dir / "review-state" / "failed-reviewer-1-round-1.state.json"
    assert not failure_state.exists()


@pytest.mark.parametrize(
    ("expected_error", "failure_kind"),
    [
        (
            NodeExecutionError("reviewer node execution failed"),
            "node_execution_failed",
        ),
        (
            WorkspaceSetupError(
                "reviewer workspace setup failed",
                {"status": "failed"},
            ),
            "workspace_setup_failed",
        ),
    ],
)
def test_expected_reviewer_execution_failures_respect_continue_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expected_error: Exception,
    failure_kind: str,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = _node().model_copy(
        update={"execution_policy": ExecutionPolicy(continue_on_failure=True)}
    )
    node_dir = output.create_node_dir(node_artifact_request(node.id))

    async def fake_guard(request):
        if request.provider.provider == "failed":
            raise expected_error
        assert request.invocation_output_file is not None
        request.invocation_output_file.write_text(review_output(), encoding="utf-8")
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

    result = asyncio.run(review_loop_rounds.run_reviewer_round(request))

    assert [artifact.task_id for artifact in result.outputs] == ["ok_reviewer_0"]
    assert result.reviewer_failure_count == 1
    failure_state = node_dir / "review-state" / "failed-reviewer-1-round-1.state.json"
    assert failure_state.exists()
    payload = json.loads(failure_state.read_text(encoding="utf-8"))
    assert payload["evaluation_kind"] == "reviewer_failure"
    assert payload["failure_kind"] == failure_kind
    assert payload["failure_message"] == str(expected_error)


def test_multiple_reviewer_invocation_failures_raise_typed_node_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = _node()
    node_dir = output.create_node_dir(node_artifact_request(node.id))

    async def fake_guard(request):  # noqa: ARG001 - Test double callback signature.
        raise _provider_failure("quota_or_rate_limit", "provider_transport")

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
            provider("first", ProviderRole.REVIEWER, "first_reviewer_0"),
            provider("second", ProviderRole.REVIEWER, "second_reviewer_1"),
        ),
        artifact_dir=node_dir,
        reviewer_prompt_context="Review task.",
        review_context="Candidate",
        previous_review_packet=None,
        audit_round_num=None,
        round_num=1,
    )

    with pytest.raises(NodeExecutionError, match="Reviewer invocation failed"):
        asyncio.run(review_loop_rounds.run_reviewer_round(request))

    assert (node_dir / "review-state" / "first-reviewer-0-round-1.state.json").exists()
    assert (node_dir / "review-state" / "second-reviewer-1-round-1.state.json").exists()


def test_invalid_candidate_round_skips_reviewers_and_tracks_accounting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = _node()
    node_dir = output.create_node_dir(node_artifact_request(node.id))

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
    node_dir = output.create_node_dir(node_artifact_request(node.id))
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
    node_dir = output.create_node_dir(node_artifact_request(node.id))
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
    assert result.selected_round_num == 1
    assert result.latest_executor_outputs is not None
    assert result.latest_executor_outputs[0].content == candidate


def test_remediation_context_exhaustion_discards_recovered_executor_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = _worktree_node()
    node_dir = output.create_node_dir(node_artifact_request(node.id))
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
    node_dir = output.create_node_dir(node_artifact_request(node.id))

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
    node_dir = output.create_node_dir(node_artifact_request(node.id))
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
