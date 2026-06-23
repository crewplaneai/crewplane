from __future__ import annotations

from crewplane.architecture.contracts import CommandResult, ProviderKind

from .evidence import collect_failure_evidence, failure_lines
from .formatting import fallback_summary, with_condensed_context
from .types import InvocationFailureSummary


def classify_invocation_failure(
    provider_kind: ProviderKind,
    result: CommandResult,
) -> InvocationFailureSummary:
    lines = failure_lines(result)
    evidence, candidate_lines, line_count = collect_failure_evidence(
        provider_kind=provider_kind,
        lines=lines,
    )
    if evidence:
        best = max(evidence, key=lambda item: (item.priority, item.sequence))
        return with_condensed_context(best.summary, line_count)
    return fallback_summary(candidate_lines)
