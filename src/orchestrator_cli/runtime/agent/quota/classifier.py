from __future__ import annotations

from datetime import UTC, datetime

from orchestrator_cli.core.config import AgentConfig

from ..types import CommandResult, QuotaClassification
from .evidence import collect_quota_context_lines, find_quota_evidence
from .parser_resolution import resolve_quota_parser
from .waits import extract_wait_candidates_from_line


def classify_quota(
    config: AgentConfig,
    result: CommandResult,
    cli_executable: str,
) -> QuotaClassification:
    parser = resolve_quota_parser(config, cli_executable)
    output = result.combined_output
    quota_evidence = find_quota_evidence(output, parser, config)
    if quota_evidence is None:
        return QuotaClassification(
            is_quota=False,
            reset_after_seconds=None,
            evidence=None,
        )

    now_utc = datetime.now(UTC)
    context_lines = collect_quota_context_lines(output, parser, config)
    wait_candidates: list[float] = []
    for line in context_lines:
        wait_candidates.extend(extract_wait_candidates_from_line(line, now_utc))

    reset_after_seconds = max(wait_candidates) if wait_candidates else None
    return QuotaClassification(
        is_quota=True,
        reset_after_seconds=reset_after_seconds,
        evidence=quota_evidence,
    )


def compute_quota_wait_seconds(
    config: AgentConfig, quota: QuotaClassification
) -> float:
    configured_delay = config.quota_reached_retry_delay_seconds
    if quota.reset_after_seconds is None:
        return configured_delay
    parsed_wait = max(
        quota.reset_after_seconds + config.quota_reset_sleep_floor_seconds,
        config.quota_reset_sleep_floor_seconds,
    )
    return max(parsed_wait, configured_delay)
