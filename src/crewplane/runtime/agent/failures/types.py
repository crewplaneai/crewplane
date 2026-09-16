from __future__ import annotations

from pathlib import Path

from crewplane.architecture.contracts.invocation_failures import (
    InvocationFailureSummary,
)


class InvocationFailureError(RuntimeError):
    def __init__(
        self,
        prefix: str,
        summary: InvocationFailureSummary,
        log_file: Path | None,
    ) -> None:
        self.summary = summary
        self.kind = summary.kind
        self.phase = summary.phase
        self.source = summary.source
        self.advice = summary.advice
        self.log_file = log_file
        self.last_non_quota_failure: InvocationFailureSummary | None = None
        super().__init__(f"{prefix}: {summary.format_for_error(log_file)}")
