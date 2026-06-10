from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from orchestrator_cli.core.execution_state import (
    RUN_STATUS_CANCELLED,
    RUN_STATUS_FAILED,
    RUN_STATUS_SUCCEEDED,
)

from .run_history import RunHistoryRecord

ResumeDecisionKind = Literal["skip", "resume", "execute_full"]


@dataclass(frozen=True)
class ResumeDecision:
    kind: ResumeDecisionKind
    successful_run: RunHistoryRecord | None = None
    resume_source: RunHistoryRecord | None = None


def decide_same_context_action(
    records: tuple[RunHistoryRecord, ...],
    force: bool,
) -> ResumeDecision:
    if force:
        return ResumeDecision(kind="execute_full")

    for record in records:
        if record.manifest.status == RUN_STATUS_SUCCEEDED:
            return ResumeDecision(kind="skip", successful_run=record)

    for record in records:
        if record.manifest.status in {RUN_STATUS_FAILED, RUN_STATUS_CANCELLED}:
            return ResumeDecision(kind="resume", resume_source=record)

    return ResumeDecision(kind="execute_full")
