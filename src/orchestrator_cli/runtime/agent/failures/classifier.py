from __future__ import annotations

from ..command_builder import ProviderKind
from ..types import CommandResult
from .evidence import collect_failure_evidence, failure_lines
from .formatting import fallback_summary, with_condensed_context
from .types import InvocationFailureSummary


def classify_invocation_failure(
    provider_kind: ProviderKind,
    result: CommandResult,
) -> InvocationFailureSummary:
    lines = failure_lines(result)
    evidence = collect_failure_evidence(provider_kind, lines)
    if evidence:
        best = max(evidence, key=lambda item: (item.priority, item.sequence))
        return with_condensed_context(best.summary, len(lines))
    return fallback_summary(lines)
