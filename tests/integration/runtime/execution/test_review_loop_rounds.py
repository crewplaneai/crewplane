import asyncio
import json
from pathlib import Path

import pytest

from crewplane.artifacts import OutputManager
from crewplane.core.preflight.models import (
    ExecutionPolicy,
)
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.agent.failures import (
    InvocationFailureError,
)
from crewplane.runtime.execution import NodeExecutionError
from crewplane.runtime.execution.review_loop import (
    reviewer_round as review_loop_reviewer_round,
)
from crewplane.runtime.execution.review_loop import (
    rounds as review_loop_rounds,
)
from crewplane.runtime.execution.review_loop.types import (
    ReviewerRoundRequest,
)
from crewplane.runtime.workspace.setup import WorkspaceSetupError
from tests.helpers.artifacts import node_artifact_request
from tests.integration.runtime.execution.review_loop_rounds_support import (
    make_review_node,
    make_round_runtime_context,
    provider,
    provider_failure,
    review_output,
)


def test_reviewer_outputs_are_ordered_by_declared_reviewer_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = make_review_node()
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
        runtime_context=make_round_runtime_context(),
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
    node = make_review_node()
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
        runtime_context=make_round_runtime_context(),
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


def test_parallel_reviewer_success_is_persisted_before_peer_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = make_review_node()
    node_dir = output.create_node_dir(node_artifact_request(node.id))

    async def fake_guard(request):
        if request.provider.provider == "failed":
            raise provider_failure("quota_or_rate_limit", "provider_transport")
        assert request.invocation_output_file is not None
        request.invocation_output_file.write_text(review_output(), encoding="utf-8")
        return 0

    monkeypatch.setattr(
        review_loop_reviewer_round, "run_provider_call_with_drift_guard", fake_guard
    )
    request = ReviewerRoundRequest(
        runtime_context=make_round_runtime_context(),
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
    node = make_review_node().model_copy(
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
        runtime_context=make_round_runtime_context(),
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
    node = make_review_node().model_copy(
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
        runtime_context=make_round_runtime_context(),
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
    node = make_review_node()
    node_dir = output.create_node_dir(node_artifact_request(node.id))

    async def fake_guard(request):  # noqa: ARG001 - Test double callback signature.
        raise provider_failure("quota_or_rate_limit", "provider_transport")

    monkeypatch.setattr(
        review_loop_reviewer_round, "run_provider_call_with_drift_guard", fake_guard
    )
    request = ReviewerRoundRequest(
        runtime_context=make_round_runtime_context(),
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
