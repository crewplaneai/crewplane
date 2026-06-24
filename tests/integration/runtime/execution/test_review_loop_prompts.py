from crewplane.core.preflight.models import (
    ArtifactContract,
    PreflightExecutionNode,
    ProviderRecord,
)
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.execution.review_loop.prompts import (
    build_executor_prompt,
    build_review_context,
    build_reviewer_prompt,
    resolve_previous_candidate_context,
)
from crewplane.runtime.execution.review_loop.types import ExecutorRoundArtifact


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
        artifact_contract=ArtifactContract(output_path="review.node-result.md"),
    )


def test_build_executor_prompt_adds_canonical_instruction_and_review_state() -> None:
    previous = "## Unresolved Issues\n\n- Fix the retry branch"

    prompt = build_executor_prompt(
        "Implement the feature.",
        "Previous candidate body",
        previous,
    )

    assert "Return the full revised candidate in this response." in prompt
    assert "Previous canonical candidate:\nPrevious candidate body" in prompt
    assert f"Previous unresolved review state:\n{previous}" in prompt


def test_build_executor_prompt_adds_initial_review_handoff() -> None:
    prompt = build_executor_prompt(
        "Implement the feature.",
        None,
        None,
        "Initial review found no issues.",
    )

    assert "Initial reviewer handoff:\nInitial review found no issues." in prompt
    assert "Previous unresolved review state:" not in prompt


def test_build_reviewer_prompt_includes_current_context_and_response_contract() -> None:
    prompt = build_reviewer_prompt(
        "Review task.",
        "=== exec executor output ===\nCandidate",
        "Previous unresolved issues",
    )

    assert "You are acting only as a reviewer." in prompt
    assert "Previous unresolved review state:\nPrevious unresolved issues" in prompt
    assert "Current executor output(s):" in prompt
    assert "VERDICT: CHANGES_REQUESTED | NITS_ONLY | NO_FINDINGS" in prompt


def test_build_reviewer_prompt_can_label_initial_review_context() -> None:
    prompt = build_reviewer_prompt(
        "Review task.",
        "Existing context",
        None,
        "Existing review context",
        "No same-node executor candidate exists yet.",
        (
            "You are acting only as a reviewer.\n"
            "Review only the existing context shown below."
        ),
    )

    assert "Review only the existing context shown below." in prompt
    assert "Existing review context:" in prompt
    assert "No same-node executor candidate exists yet." in prompt
    assert "Current executor output(s):" not in prompt


def test_resolve_previous_candidate_context_uses_executor_artifacts(tmp_path) -> None:
    output_file = tmp_path / "exec_executor_0_round1.md"
    artifact = ExecutorRoundArtifact(
        provider=provider("exec", ProviderRole.EXECUTOR, "exec_executor_0"),
        task_id="exec_executor_0",
        content="Candidate body",
        output_file=output_file,
    )

    context = resolve_previous_candidate_context(_node(), [artifact], None)

    assert context == build_review_context([artifact])
    assert "exec executor output" in context
    assert "Candidate body" in context
