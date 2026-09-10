import asyncio
import json
from pathlib import Path

import pytest

from crewplane.artifacts import OutputManager
from crewplane.core.file_hashing import file_size_and_sha256
from crewplane.core.preflight.models import (
    PreflightExecutionNode,
)
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.agent.failures import (
    InvocationFailureError,
)
from crewplane.runtime.execution import review_loop as review_loop_runtime
from crewplane.runtime.execution.consensus import (
    evaluate_review_output,
)
from crewplane.runtime.execution.review_loop import (
    audit_round as review_loop_audit_round,
)
from crewplane.runtime.execution.review_loop import (
    executor_round as review_loop_executor_round,
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
    ReviewerRoundRunResult,
)
from crewplane.runtime.workspace.invocation import invocation_slug
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.workspace_records import workspace_selection_record
from tests.integration.runtime.execution.review_loop_rounds_support import (
    make_review_node,
    make_round_runtime_context,
    provider,
    provider_failure,
    review_output,
)


def _worktree_node() -> PreflightExecutionNode:
    return make_review_node().model_copy(
        update={
            "workspace_policy": workspace_selection_record(
                enabled=True,
                kind="worktree",
                clean_start="strict",
                materialization="worktree_checkout",
            )
        }
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
        runtime_context=make_round_runtime_context(),
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
    slug = invocation_slug(node.id, "exec_executor_0", None, 1)
    assert node_dir / f"workspace-state-{slug}.json" in captured_allowed_paths
    assert node_dir / "workspace-bundles" / f"{slug}.bundle" in captured_allowed_paths


def test_executor_drift_guard_does_not_allow_workspace_paths_without_managed_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = make_review_node()
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
        runtime_context=make_round_runtime_context(),
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
    slug = invocation_slug(node.id, "exec_executor_0", None, 1)
    assert node_dir / f"workspace-state-{slug}.json" not in captured_allowed_paths
    assert (
        node_dir / "workspace-bundles" / f"{slug}.bundle" not in captured_allowed_paths
    )


def test_executor_round_rejects_output_changed_after_runtime_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = make_review_node()
    node_dir = output.create_node_dir(node_artifact_request(node.id))
    runtime_context = make_round_runtime_context()

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
    runtime_context = make_round_runtime_context()
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


def test_invalid_candidate_round_skips_reviewers_and_tracks_accounting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = make_review_node()
    node_dir = output.create_node_dir(node_artifact_request(node.id))

    async def fail_review(request):  # noqa: ARG001 - Test double callback signature.
        raise AssertionError("reviewer should not run for an invalid candidate")

    monkeypatch.setattr(review_loop_audit_round, "run_reviewer_round", fail_review)
    request = AuditRoundRequest(
        runtime_context=make_round_runtime_context(),
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
    node = make_review_node()
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
        runtime_context=make_round_runtime_context(),
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
    node = make_review_node()
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
        raise provider_failure(
            "provider_session_context_exhausted",
            "provider_session",
        )

    monkeypatch.setattr(review_loop_audit_round, "run_reviewer_round", fake_review)
    monkeypatch.setattr(review_loop_audit_round, "run_executor_round", fail_executor)
    request = AuditRoundRequest(
        runtime_context=make_round_runtime_context(),
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
        node_dir / f"workspace-state-{invocation_slug(node.id, task_id, None, 2)}.json"
        for task_id in ("fast_executor_0", "failed_executor_1")
    ]

    async def fake_review(request):  # noqa: ARG001 - Test double callback signature.
        return ReviewerRoundRunResult(
            outputs=[_reviewer_artifact(node_dir, "- Fix the retry branch")],
            drift_warning_count=0,
        )

    async def fail_executor(request):  # noqa: ARG001 - Test double callback signature.
        for state_path in state_paths:
            _write_lineage_state(state_path)
        raise provider_failure(
            "provider_session_context_exhausted",
            "provider_session",
        )

    monkeypatch.setattr(review_loop_audit_round, "run_reviewer_round", fake_review)
    monkeypatch.setattr(review_loop_audit_round, "run_executor_round", fail_executor)
    request = AuditRoundRequest(
        runtime_context=make_round_runtime_context(),
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
    node = make_review_node()
    node_dir = output.create_node_dir(node_artifact_request(node.id))

    async def fake_review(request):  # noqa: ARG001 - Test double callback signature.
        return ReviewerRoundRunResult(
            outputs=[_reviewer_artifact(node_dir, "- Fix the retry branch")],
            drift_warning_count=0,
        )

    async def fail_executor(request):  # noqa: ARG001 - Test double callback signature.
        raise provider_failure("quota_or_rate_limit", "provider_transport")

    monkeypatch.setattr(review_loop_audit_round, "run_reviewer_round", fake_review)
    monkeypatch.setattr(review_loop_audit_round, "run_executor_round", fail_executor)
    request = AuditRoundRequest(
        runtime_context=make_round_runtime_context(),
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
    slug = invocation_slug(node.id, "exec_executor_0", None, 2)
    state_path = node_dir / f"workspace-state-{slug}.json"

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
        runtime_context=make_round_runtime_context(),
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
