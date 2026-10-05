"""Report audit review stalls and determine consensus after completed reviews."""

from __future__ import annotations

from ..common import execution_console, should_print_console
from ..consensus import check_consensus
from .types import AuditRoundProgress, AuditRoundRequest, ReviewRoundState
from .validation import count_unresolved_review_issues, emit_review_stall_warning


def emit_review_stall_warning_if_needed(
    request: AuditRoundRequest,
    progress: AuditRoundProgress,
    round_state: ReviewRoundState,
    round_num: int,
) -> None:
    """Compare reviewed candidates and issues only when a prior fingerprint exists."""
    if progress.previous_executor_fingerprint is None:
        return
    emit_review_stall_warning(
        telemetry=request.telemetry,
        node_id=request.stage.id,
        audit_round_num=request.audit_round_num,
        round_num=round_num,
        previous_unresolved_fingerprints=progress.previous_unresolved_fingerprints,
        current_unresolved_fingerprints=round_state.current_unresolved_fingerprints,
        current_unresolved_issue_count=count_unresolved_review_issues(
            round_state.reviewer_outputs
        ),
        previous_executor_fingerprint=progress.previous_executor_fingerprint,
        current_executor_fingerprint=round_state.current_executor_fingerprint,
    )


def review_phase_reached_consensus(
    request: AuditRoundRequest,
    round_state: ReviewRoundState,
    round_num: int,
) -> bool:
    """Return whether every review approves and no reviewer invocation failed.

    Print the consensus message only on approval when console output is enabled.
    This does not mutate audit progress.
    """
    if round_state.reviewer_failure_count > 0:
        return False
    if not check_consensus(
        [artifact.evaluation for artifact in round_state.reviewer_outputs]
    ):
        return False
    if should_print_console(request.telemetry):
        execution_console(request.telemetry).print(
            f"[green bold]Consensus reached in round {round_num}![/]"
        )
    return True
