from __future__ import annotations

from .patterns import (
    ADVICE_BY_KIND,
    FAILURE_SUMMARY_HINTS,
    FAILURE_SUMMARY_MAX_CHARS,
    FAILURE_SUMMARY_NOISE_PREFIXES,
)
from .types import FailureSource, InvocationFailureSummary


def is_failure_hint(text: str) -> bool:
    lowered = text.casefold()
    return any(hint in lowered for hint in FAILURE_SUMMARY_HINTS)


def with_condensed_context(
    summary: InvocationFailureSummary,
    line_count: int,
) -> InvocationFailureSummary:
    if line_count <= 1 or summary.condensed:
        return summary
    return InvocationFailureSummary(
        kind=summary.kind,
        phase=summary.phase,
        source=summary.source,
        message=summary.message,
        advice=summary.advice,
        condensed=True,
    )


def fallback_summary(
    lines: list[tuple[str, FailureSource]],
) -> InvocationFailureSummary:
    if not lines:
        return _unknown_summary("No output captured.", "none", False)
    candidate_lines = _candidate_lines(lines)
    selected = _preferred_failure_line(candidate_lines)
    if selected is None:
        selected = candidate_lines[-1] if candidate_lines else lines[-1]
    message, source = selected
    clipped, was_clipped = clip_failure_summary(message)
    return InvocationFailureSummary(
        kind="unknown_provider_error",
        phase="unknown",
        source=source,
        message=clipped,
        advice=ADVICE_BY_KIND["unknown_provider_error"],
        condensed=was_clipped or len(lines) > 1,
    )


def _unknown_summary(
    message: str,
    source: FailureSource,
    condensed: bool,
) -> InvocationFailureSummary:
    return InvocationFailureSummary(
        kind="unknown_provider_error",
        phase="unknown",
        source=source,
        message=message,
        advice=ADVICE_BY_KIND["unknown_provider_error"],
        condensed=condensed,
    )


def _candidate_lines(
    lines: list[tuple[str, FailureSource]],
) -> list[tuple[str, FailureSource]]:
    candidates = [
        item
        for item in lines
        if not _looks_like_stack_frame(item[0]) and item[0] not in {"[", "]", "{", "}"}
    ]
    return candidates or lines


def _preferred_failure_line(
    lines: list[tuple[str, FailureSource]],
) -> tuple[str, FailureSource] | None:
    for line, source in reversed(lines):
        if is_failure_hint(line):
            return line, source
    for line, source in reversed(lines):
        line_lower = line.casefold()
        if any(
            line_lower.startswith(prefix) for prefix in FAILURE_SUMMARY_NOISE_PREFIXES
        ):
            continue
        return line, source
    return None


def clip_failure_summary(text: str) -> tuple[str, bool]:
    if len(text) <= FAILURE_SUMMARY_MAX_CHARS:
        return text, False
    return f"{text[: FAILURE_SUMMARY_MAX_CHARS - 3].rstrip()}...", True


def _looks_like_stack_frame(line: str) -> bool:
    stripped = line.strip()
    return (
        stripped.startswith("at ")
        or stripped.startswith("File ")
        or stripped.startswith("Traceback ")
    )
