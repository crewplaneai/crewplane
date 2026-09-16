from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from itertools import chain

from crewplane.architecture.contracts import (
    CommandResult,
    QuotaClassification,
)

from .evidence import collect_quota_context_lines, find_quota_evidence
from .waits import extract_wait_candidates_from_line


def classify_generic_quota(
    result: CommandResult,
    configured_hints: tuple[str, ...],
    now_utc: datetime,
    family_hints: tuple[str, ...] = (),
) -> QuotaClassification:
    quota_evidence = find_quota_evidence(
        _result_lines_for_quota(result),
        family_hints,
        configured_hints,
    )
    if quota_evidence is None:
        return QuotaClassification(
            is_quota=False,
            reset_after_seconds=None,
            evidence=None,
        )

    context_lines = collect_quota_context_lines(
        _result_lines_for_quota(result),
        family_hints,
        configured_hints,
    )
    wait_candidates: list[float] = []
    for line in context_lines:
        wait_candidates.extend(extract_wait_candidates_from_line(line, now_utc))

    reset_after_seconds = max(wait_candidates) if wait_candidates else None
    return QuotaClassification(
        is_quota=True,
        reset_after_seconds=reset_after_seconds,
        evidence=quota_evidence,
    )


def _result_lines_for_quota(result: CommandResult) -> Iterable[str]:
    if _has_successful_stdout(result):
        return result.iter_stdout_lines()
    return chain(result.iter_stderr_lines(), result.iter_stdout_lines())


def _has_successful_stdout(result: CommandResult) -> bool:
    if result.returncode != 0:
        return False
    return any(line.strip() for line in result.iter_stdout_lines())
