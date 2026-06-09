from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass

from orchestrator_cli.architecture.contracts import CommandResult, QuotaParserProfile
from orchestrator_cli.core.config import AgentConfig

from ..quota import classify_quota, compute_quota_wait_seconds
from ..retry_units import format_wait_duration
from .state import InvocationDiagnosticNotice

QUOTA_RETRY_GUARD_HOURS = 5
QUOTA_RETRY_GUARD_SECONDS = QUOTA_RETRY_GUARD_HOURS * 60 * 60


@dataclass(frozen=True)
class NoFailureRetry:
    retry_count: int
    retry_matched: bool


@dataclass(frozen=True)
class ScheduleFailureRetry:
    retry_count: int
    wait_seconds: float
    notice: InvocationDiagnosticNotice


type FailureRetryDecision = NoFailureRetry | ScheduleFailureRetry


@dataclass(frozen=True)
class NoQuotaRetry:
    quota_retry_started_at: float | None
    quota_retry_count: int


@dataclass(frozen=True)
class ScheduleQuotaRetry:
    quota_retry_started_at: float
    quota_retry_count: int
    wait_seconds: float
    notice: InvocationDiagnosticNotice


@dataclass(frozen=True)
class QuotaRetryFailure:
    quota_retry_started_at: float
    quota_retry_count: int
    message: str


type QuotaRetryDecision = NoQuotaRetry | ScheduleQuotaRetry | QuotaRetryFailure


def evaluate_failure_retry(
    config: AgentConfig,
    cmd: list[str],
    result: CommandResult,
    retry_count: int,
) -> FailureRetryDecision:
    retry_failure = should_retry_failure(config, result)
    if not retry_failure or retry_count >= config.max_retries:
        return NoFailureRetry(
            retry_count=retry_count,
            retry_matched=retry_failure,
        )

    retry_count += 1
    if result.returncode != 0:
        failure_detail = f"failed with exit code {result.returncode}"
    else:
        failure_detail = "matched configured retry conditions"
    return ScheduleFailureRetry(
        retry_count=retry_count,
        wait_seconds=config.retry_delay_seconds,
        notice=InvocationDiagnosticNotice(
            level="warning",
            message=(
                f"{cmd[0]} {failure_detail}; retrying in "
                f"{config.retry_delay_seconds}s "
                f"({retry_count}/{config.max_retries})"
            ),
            operation="retry_scheduled",
            attributes={
                "retry_count": retry_count,
                "max_retries": config.max_retries,
                "retry_delay_seconds": round(config.retry_delay_seconds, 3),
                "returncode": result.returncode,
            },
            console_message=(
                "[yellow]WARN[/] "
                f"{cmd[0]} {failure_detail}; "
                f"retrying in {config.retry_delay_seconds}s "
                f"({retry_count}/{config.max_retries})"
            ),
        ),
    )


def evaluate_quota_retry(
    config: AgentConfig,
    cmd: list[str],
    quota_parser: QuotaParserProfile,
    result: CommandResult,
    quota_retry_started_at: float | None,
    quota_retry_count: int,
) -> QuotaRetryDecision:
    quota = classify_quota(config, result, quota_parser)
    if not quota.is_quota:
        return NoQuotaRetry(
            quota_retry_started_at=quota_retry_started_at,
            quota_retry_count=quota_retry_count,
        )

    if quota_retry_started_at is None:
        quota_retry_started_at = time.monotonic()
    elif quota_retry_guard_exhausted(quota_retry_started_at):
        return QuotaRetryFailure(
            quota_retry_started_at=quota_retry_started_at,
            quota_retry_count=quota_retry_count,
            message=(
                "Quota retry guard exceeded after "
                f"{QUOTA_RETRY_GUARD_HOURS} hours for {cmd[0]}"
            ),
        )

    if (
        quota.reset_after_seconds is not None
        and quota.reset_after_seconds > QUOTA_RETRY_GUARD_SECONDS
    ):
        return QuotaRetryFailure(
            quota_retry_started_at=quota_retry_started_at,
            quota_retry_count=quota_retry_count,
            message=(
                "Quota reached for "
                f"{cmd[0]}; provider reported reset after "
                f"{format_wait_duration(quota.reset_after_seconds)}, which exceeds "
                f"{QUOTA_RETRY_GUARD_HOURS} hours"
            ),
        )

    wait_seconds = compute_quota_wait_seconds(config, quota)
    if quota_retry_guard_will_exhaust(quota_retry_started_at, wait_seconds):
        elapsed_seconds = quota_retry_elapsed_seconds(quota_retry_started_at)
        return QuotaRetryFailure(
            quota_retry_started_at=quota_retry_started_at,
            quota_retry_count=quota_retry_count,
            message=(
                "Quota retry guard would exceed "
                f"{QUOTA_RETRY_GUARD_HOURS} hours for {cmd[0]}; "
                f"elapsed retry time is {format_wait_duration(elapsed_seconds)} and "
                f"next retry wait is {format_wait_duration(wait_seconds)}"
            ),
        )

    quota_retry_count += 1
    if quota.reset_after_seconds is None:
        wait_detail = f"{format_wait_duration(wait_seconds)} (configured fixed delay)"
    else:
        wait_detail = (
            f"{format_wait_duration(wait_seconds)} "
            f"(parsed reset {format_wait_duration(quota.reset_after_seconds)} "
            f"+ floor {config.quota_reset_sleep_floor_seconds}s)"
        )
    return ScheduleQuotaRetry(
        quota_retry_started_at=quota_retry_started_at,
        quota_retry_count=quota_retry_count,
        wait_seconds=wait_seconds,
        notice=InvocationDiagnosticNotice(
            level="warning",
            message=(
                f"Quota reached for {cmd[0]}; retrying in {wait_detail} "
                f"(quota attempt {quota_retry_count})"
            ),
            operation="quota_retry_scheduled",
            attributes={
                "quota_attempt": quota_retry_count,
                "wait_seconds": round(wait_seconds, 3),
            },
            console_message=(
                "[yellow]WARN[/] Quota reached for "
                f"{cmd[0]}; retrying in {wait_detail} "
                f"(quota attempt {quota_retry_count})"
            ),
        ),
    )


def should_retry_failure(config: AgentConfig, result: CommandResult) -> bool:
    return (
        result.returncode in config.retry_on_exit_codes
        or matches_any(result.iter_stderr_lines(), config.retry_on_stderr_contains)
        or matches_any(result.iter_combined_lines(), config.retry_on_output_contains)
    )


def matches_any(lines: Iterable[str], needles: list[str]) -> bool:
    if not needles:
        return False
    normalized_needles = [needle.lower() for needle in needles if needle]
    if not normalized_needles:
        return False
    for line in lines:
        line_lower = line.lower()
        if any(needle in line_lower for needle in normalized_needles):
            return True
    return False


def quota_retry_guard_exhausted(started_at: float | None) -> bool:
    if started_at is None:
        return False
    return quota_retry_elapsed_seconds(started_at) >= QUOTA_RETRY_GUARD_SECONDS


def quota_retry_elapsed_seconds(started_at: float) -> float:
    return max(0.0, time.monotonic() - started_at)


def quota_retry_guard_will_exhaust(
    started_at: float,
    wait_seconds: float,
) -> bool:
    elapsed = quota_retry_elapsed_seconds(started_at)
    return elapsed + wait_seconds >= QUOTA_RETRY_GUARD_SECONDS
