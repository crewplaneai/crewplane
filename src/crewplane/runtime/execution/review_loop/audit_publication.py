"""Complete audit publications before releasing their caller's state ownership."""

from __future__ import annotations

from collections.abc import Callable

from .audit_io import complete_audit_io
from .state import persist_review_inbox, render_review_inbox
from .types import AuditRoundProgress, AuditRoundRequest, ReviewerRoundArtifact


async def complete_audit_publication(publish: Callable[[], object]) -> None:
    """Offload publication and drain its worker before propagating cancellation.

    The caller must retain ownership of publication inputs until this returns.
    Publication errors take precedence over cancellation, as synchronous writes
    did. Repeated cancellation cannot leave a worker writing during cleanup.
    """
    await complete_audit_io(publish)


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
