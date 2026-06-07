from __future__ import annotations

from typing import assert_never

from .retry import (
    FailureRetryDecision,
    NoFailureRetry,
    NoQuotaRetry,
    QuotaRetryDecision,
    QuotaRetryFailure,
    ScheduleFailureRetry,
    ScheduleQuotaRetry,
)
from .state import (
    ContinueAttemptTransition,
    ExtractedInvocationOutput,
    FinalizeSuccessAttemptTransition,
    InvocationAttemptResult,
    InvocationRetryCursor,
    RaiseFailedExitAttemptTransition,
    RaiseOutputExtractionFailureAttemptTransition,
    RaiseQuotaFailureAttemptTransition,
    RaiseRetryExhaustedAttemptTransition,
    SleepAndRetryAttemptTransition,
)


def transition_from_structured_output(
    attempt_result: InvocationAttemptResult,
    cursor: InvocationRetryCursor,
    failure_retry_decision: FailureRetryDecision | None,
) -> (
    ContinueAttemptTransition
    | SleepAndRetryAttemptTransition
    | RaiseRetryExhaustedAttemptTransition
    | FinalizeSuccessAttemptTransition
):
    if attempt_result.extracted_output is None:
        return ContinueAttemptTransition(
            retry_count=cursor.retry_count,
            quota_retry_count=cursor.quota_retry_count,
            quota_retry_started_at=cursor.quota_retry_started_at,
            attempt_output_for_usage=attempt_result.usage_output,
        )
    if isinstance(failure_retry_decision, ScheduleFailureRetry):
        return SleepAndRetryAttemptTransition(
            retry_count=failure_retry_decision.retry_count,
            quota_retry_count=cursor.quota_retry_count,
            quota_retry_started_at=cursor.quota_retry_started_at,
            retry_delay_seconds=failure_retry_decision.wait_seconds,
            extracted_output=attempt_result.extracted_output,
            attempt_output_for_usage=attempt_result.usage_output,
            notice=failure_retry_decision.notice,
        )
    if isinstance(failure_retry_decision, NoFailureRetry):
        if failure_retry_decision.retry_matched:
            return RaiseRetryExhaustedAttemptTransition(
                retry_count=failure_retry_decision.retry_count,
                quota_retry_count=cursor.quota_retry_count,
                quota_retry_started_at=cursor.quota_retry_started_at,
                extracted_output=attempt_result.extracted_output,
            )
        return FinalizeSuccessAttemptTransition(
            retry_count=failure_retry_decision.retry_count,
            quota_retry_count=cursor.quota_retry_count,
            quota_retry_started_at=cursor.quota_retry_started_at,
            extracted_output=attempt_result.extracted_output,
        )
    raise RuntimeError("Structured output transition requires a retry decision.")


def transition_from_quota_retry(
    attempt_result: InvocationAttemptResult,
    cursor: InvocationRetryCursor,
    quota_retry_decision: QuotaRetryDecision,
) -> (
    ContinueAttemptTransition
    | SleepAndRetryAttemptTransition
    | RaiseQuotaFailureAttemptTransition
):
    if isinstance(quota_retry_decision, NoQuotaRetry):
        return ContinueAttemptTransition(
            retry_count=cursor.retry_count,
            quota_retry_count=quota_retry_decision.quota_retry_count,
            quota_retry_started_at=quota_retry_decision.quota_retry_started_at,
            attempt_output_for_usage=attempt_result.usage_output,
        )
    if isinstance(quota_retry_decision, ScheduleQuotaRetry):
        return SleepAndRetryAttemptTransition(
            retry_count=cursor.retry_count,
            quota_retry_count=quota_retry_decision.quota_retry_count,
            quota_retry_started_at=quota_retry_decision.quota_retry_started_at,
            retry_delay_seconds=quota_retry_decision.wait_seconds,
            attempt_output_for_usage=attempt_result.usage_output,
            notice=quota_retry_decision.notice,
        )
    if isinstance(quota_retry_decision, QuotaRetryFailure):
        return RaiseQuotaFailureAttemptTransition(
            retry_count=cursor.retry_count,
            quota_retry_count=quota_retry_decision.quota_retry_count,
            quota_retry_started_at=quota_retry_decision.quota_retry_started_at,
            message=quota_retry_decision.message,
            attempt_output_for_usage=attempt_result.usage_output,
        )
    assert_never(quota_retry_decision)


def transition_from_retryable_failure(
    attempt_result: InvocationAttemptResult,
    cursor: InvocationRetryCursor,
    failure_retry_decision: FailureRetryDecision,
) -> ContinueAttemptTransition | SleepAndRetryAttemptTransition:
    if isinstance(failure_retry_decision, ScheduleFailureRetry):
        return SleepAndRetryAttemptTransition(
            retry_count=failure_retry_decision.retry_count,
            quota_retry_count=cursor.quota_retry_count,
            quota_retry_started_at=cursor.quota_retry_started_at,
            retry_delay_seconds=failure_retry_decision.wait_seconds,
            attempt_output_for_usage=attempt_result.usage_output,
            notice=failure_retry_decision.notice,
        )
    if isinstance(failure_retry_decision, NoFailureRetry):
        return ContinueAttemptTransition(
            retry_count=failure_retry_decision.retry_count,
            quota_retry_count=cursor.quota_retry_count,
            quota_retry_started_at=cursor.quota_retry_started_at,
            attempt_output_for_usage=attempt_result.usage_output,
        )
    assert_never(failure_retry_decision)


def transition_from_terminal_failure(
    attempt_result: InvocationAttemptResult,
    cursor: InvocationRetryCursor,
    retry_matched: bool,
) -> (
    ContinueAttemptTransition
    | RaiseFailedExitAttemptTransition
    | RaiseRetryExhaustedAttemptTransition
):
    if attempt_result.result.returncode != 0:
        return RaiseFailedExitAttemptTransition(
            retry_count=cursor.retry_count,
            quota_retry_count=cursor.quota_retry_count,
            quota_retry_started_at=cursor.quota_retry_started_at,
            attempt_output_for_usage=attempt_result.usage_output,
        )
    if retry_matched:
        return RaiseRetryExhaustedAttemptTransition(
            retry_count=cursor.retry_count,
            quota_retry_count=cursor.quota_retry_count,
            quota_retry_started_at=cursor.quota_retry_started_at,
            attempt_output_for_usage=attempt_result.usage_output,
        )
    return ContinueAttemptTransition(
        retry_count=cursor.retry_count,
        quota_retry_count=cursor.quota_retry_count,
        quota_retry_started_at=cursor.quota_retry_started_at,
        attempt_output_for_usage=attempt_result.usage_output,
    )


def transition_from_final_extraction(
    attempt_result: InvocationAttemptResult,
    cursor: InvocationRetryCursor,
    extracted_output: ExtractedInvocationOutput,
) -> RaiseOutputExtractionFailureAttemptTransition | FinalizeSuccessAttemptTransition:
    if extracted_output.output_extraction_status != "success":
        return RaiseOutputExtractionFailureAttemptTransition(
            retry_count=cursor.retry_count,
            quota_retry_count=cursor.quota_retry_count,
            quota_retry_started_at=cursor.quota_retry_started_at,
            attempt_output_for_usage=attempt_result.usage_output,
            extracted_output=extracted_output,
        )
    return FinalizeSuccessAttemptTransition(
        retry_count=cursor.retry_count,
        quota_retry_count=cursor.quota_retry_count,
        quota_retry_started_at=cursor.quota_retry_started_at,
        extracted_output=extracted_output,
    )
