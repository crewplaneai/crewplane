"""Publish audit review inboxes from settled review state."""

from __future__ import annotations

from .state import persist_review_inbox, render_review_inbox
from .types import AuditRoundProgress, AuditRoundRequest, ReviewerRoundArtifact


def persist_round_review_inbox(
    request: AuditRoundRequest,
    progress: AuditRoundProgress,
    reviewer_outputs: list[ReviewerRoundArtifact],
    round_num: int,
) -> None:
    """Synchronously publish unresolved feedback and current/previous candidates.

    Rounds without executor feedback leave the inbox untouched. Storage errors
    propagate; progress is read without mutation.
    """
    inbox_markdown = render_review_inbox(
        node_id=request.stage.id,
        audit_round_num=request.audit_round_num,
        round_num=round_num,
        executor_outputs=progress.executor_outputs,
        previous_executor_outputs=progress.previous_executor_outputs,
        reviewer_outputs=reviewer_outputs,
    )
    if inbox_markdown is not None:
        persist_review_inbox(request.audit_dir, round_num, inbox_markdown)
