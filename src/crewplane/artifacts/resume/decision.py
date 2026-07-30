from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..run_history import RunHistoryRecord

ResumeDecisionKind = Literal["skip", "resume", "execute_full"]


@dataclass(frozen=True)
class ResumeDecision:
    kind: ResumeDecisionKind
    successful_run: RunHistoryRecord | None = None
    resume_source: RunHistoryRecord | None = None
