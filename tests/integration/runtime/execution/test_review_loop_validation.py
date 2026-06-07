from pathlib import Path

from orchestrator_cli.artifacts.failure_artifacts import (
    build_invocation_failure_artifact,
)
from orchestrator_cli.core.workflow_models import ProviderSpec
from orchestrator_cli.runtime.execution.review_loop.types import (
    INVALID_CANDIDATE_EMPTY,
    INVALID_CANDIDATE_REDIRECTED,
    ExecutorRoundArtifact,
)
from orchestrator_cli.runtime.execution.review_loop.validation import (
    build_executor_output_fingerprint,
    classify_executor_output,
    validate_executor_outputs,
)


def _artifact(task_id: str, content: str) -> ExecutorRoundArtifact:
    return ExecutorRoundArtifact(
        provider=ProviderSpec(provider="exec", role="executor"),
        task_id=task_id,
        content=content,
        output_file=Path(f"{task_id}_round1.md"),
    )


def test_empty_missing_and_synthetic_failure_outputs_are_invalid() -> None:
    assert classify_executor_output("") == INVALID_CANDIDATE_EMPTY
    assert (
        classify_executor_output(
            build_invocation_failure_artifact("exec", "task", "failed")
        )
        == INVALID_CANDIDATE_EMPTY
    )


def test_redirect_only_and_status_note_outputs_are_invalid() -> None:
    assert (
        classify_executor_output(
            "Updated exec_executor_0_round1.md with the changes.\n\nDone."
        )
        == INVALID_CANDIDATE_REDIRECTED
    )


def test_mixed_commentary_with_substantive_candidate_output_is_valid() -> None:
    content = (
        "Updated exec_executor_0_round1.md for context.\n\n"
        "# Candidate Design\n\n"
        "This implementation keeps the runtime state on disk, preserves explicit "
        "failure handling, and includes enough detail for reviewers to evaluate the "
        "actual candidate instead of a redirect note."
    )

    assert classify_executor_output(content) is None


def test_validate_executor_outputs_reports_sorted_invalid_task_ids() -> None:
    result = validate_executor_outputs(
        [
            _artifact("b", "   "),
            _artifact("a", "See exec_executor_0_round1.md for the update."),
        ]
    )

    assert not result.valid
    assert result.reason == INVALID_CANDIDATE_EMPTY
    assert result.invalid_task_ids == ("b",)


def test_executor_fingerprint_is_whitespace_normalized() -> None:
    first = [_artifact("exec_executor_0", "Candidate\n\nwith   spacing")]
    second = [_artifact("exec_executor_0", "Candidate with spacing")]

    assert build_executor_output_fingerprint(
        first
    ) == build_executor_output_fingerprint(second)
