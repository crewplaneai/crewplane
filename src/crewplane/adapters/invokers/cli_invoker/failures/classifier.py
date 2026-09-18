from __future__ import annotations

from crewplane.architecture.contracts import CommandResult
from crewplane.architecture.contracts.invocation_failures import (
    InvocationFailureSummary,
)

from .evidence import collect_failure_evidence, failure_lines
from .formatting import fallback_summary, with_condensed_context

GENERIC_QUOTA_PATTERNS = (
    "usage limit reached",
    "usage limit exceeded",
    "resource exhausted",
    "resource_exhausted",
    "resource-exhausted",
    "quota reached",
    "quota exceeded",
    "rate limit reached",
    "rate limit exceeded",
    "too many requests",
    "429",
)


def classify_generic_failure(
    result: CommandResult,
    quota_patterns: tuple[str, ...] = GENERIC_QUOTA_PATTERNS,
) -> InvocationFailureSummary:
    lines = failure_lines(result)
    evidence, candidate_lines, line_count = collect_failure_evidence(
        quota_patterns=quota_patterns,
        lines=lines,
    )
    if evidence:
        best = max(evidence, key=lambda item: (item.priority, item.sequence))
        return with_condensed_context(best.summary, line_count)
    return fallback_summary(candidate_lines)
