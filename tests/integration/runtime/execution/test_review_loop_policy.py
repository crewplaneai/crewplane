from pathlib import Path

import pytest

from crewplane.core.preflight.models import (
    ArtifactContract,
    ExecutionPolicy,
    PreflightExecutionNode,
    ProviderRecord,
)
from crewplane.runtime.execution.review_loop.policy import (
    audit_round_context,
    audit_round_dir,
    consensus_failure_allows_continuation,
    resolve_audit_rounds,
    resolve_remediation_depth,
    split_sequential_review_loop_providers,
)


def _node(
    depth: int | None = None,
    audit_rounds: int | None = None,
    continue_on_failure: bool = False,
) -> PreflightExecutionNode:
    return PreflightExecutionNode(
        id="review.node",
        mode="sequential",
        execution_policy=ExecutionPolicy(
            depth=depth,
            audit_rounds=audit_rounds,
            continue_on_failure=continue_on_failure,
        ),
        artifact_contract=ArtifactContract(output_path="review.node-result.md"),
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


def test_split_sequential_review_loop_providers_requires_contiguous_roles() -> None:
    providers = [
        provider("exec-a", "executor", "exec_a_executor_0"),
        provider("review", "reviewer", "review_reviewer_0"),
        provider("exec-b", "executor", "exec_b_executor_1"),
    ]

    with pytest.raises(ValueError, match="contiguous executor segment"):
        split_sequential_review_loop_providers("review.node", providers)


def test_split_sequential_review_loop_providers_returns_executor_and_reviewer_segments() -> (
    None
):
    providers = [
        provider("exec-a", "executor", "exec_a_executor_0"),
        provider("exec-b", "executor", "exec_b_executor_1"),
        provider("review-a", "reviewer", "review_a_reviewer_0"),
        provider("review-b", "reviewer", "review_b_reviewer_1"),
    ]

    executors, reviewers = split_sequential_review_loop_providers(
        "review.node",
        providers,
    )

    assert [provider.provider for provider in executors] == ["exec-a", "exec-b"]
    assert [provider.provider for provider in reviewers] == ["review-a", "review-b"]


def test_resolve_depth_and_audit_round_defaults_and_validation() -> None:
    assert resolve_remediation_depth(_node()) == 1
    assert resolve_audit_rounds(_node()) == 1
    assert resolve_remediation_depth(_node(depth=3)) == 3
    assert resolve_audit_rounds(_node(audit_rounds=2)) == 2

    with pytest.raises(ValueError, match="depth must be greater than 0"):
        resolve_remediation_depth(_node(depth=0))
    with pytest.raises(ValueError, match="audit_rounds must be greater than 0"):
        resolve_audit_rounds(_node(audit_rounds=0))


def test_audit_round_context_and_dir_are_only_nested_for_multi_audit_rounds(
    tmp_path: Path,
) -> None:
    assert audit_round_context(1, 1) is None
    assert audit_round_dir(tmp_path, 1, 1) == tmp_path

    audit_dir = audit_round_dir(tmp_path, 2, 2)

    assert audit_round_context(2, 2) == 2
    assert audit_dir == tmp_path / "review-audit-round-2"
    assert audit_dir.exists()


def test_consensus_failure_continuation_policy_respects_node_and_global_settings() -> (
    None
):
    node = _node()

    assert consensus_failure_allows_continuation(node, "fatal") == (
        False,
        "settings.sequential_consensus_on_exhaustion=fatal",
    )

    assert consensus_failure_allows_continuation(
        _node(continue_on_failure=True),
        "fatal",
    ) == (
        True,
        "continue_on_failure=true",
    )
