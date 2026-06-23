from crewplane.architecture.contracts import CommandResult
from crewplane.runtime.agent.invocation.retry import (
    NoFailureRetry,
    QuotaRetryFailure,
    ScheduleFailureRetry,
    ScheduleQuotaRetry,
)
from crewplane.runtime.agent.invocation.state import (
    ExtractedInvocationOutput,
    InvocationAttemptResult,
    InvocationDiagnosticNotice,
    InvocationRetryCursor,
    RaiseFailedExitAttemptTransition,
    RaiseQuotaFailureAttemptTransition,
    RaiseRetryExhaustedAttemptTransition,
    SleepAndRetryAttemptTransition,
)
from crewplane.runtime.agent.invocation.transitions import (
    transition_from_quota_retry,
    transition_from_structured_output,
    transition_from_terminal_failure,
)
from crewplane.runtime.agent.usage import ParsedProviderUsage


def test_structured_output_retry_maps_to_sleep_action() -> None:
    attempt_result = InvocationAttemptResult(
        result=CommandResult(returncode=0, stdout_text="retry", stderr_text=""),
        extracted_output=ExtractedInvocationOutput(
            output_text="retry",
            output_extraction_status="success",
            parsed_provider_usage=ParsedProviderUsage(status="none"),
        ),
        usage_output="retry",
    )
    notice = InvocationDiagnosticNotice(
        level="warning",
        message="retry",
        operation="retry_scheduled",
    )

    transition = transition_from_structured_output(
        attempt_result=attempt_result,
        cursor=InvocationRetryCursor(
            retry_count=0,
            quota_retry_count=0,
            quota_retry_started_at=None,
        ),
        failure_retry_decision=ScheduleFailureRetry(
            retry_count=1,
            wait_seconds=2,
            notice=notice,
        ),
    )

    assert isinstance(transition, SleepAndRetryAttemptTransition)
    assert transition.retry_delay_seconds == 2
    assert transition.extracted_output is attempt_result.extracted_output
    assert transition.notice is notice


def test_structured_output_exhausted_match_maps_to_retry_exhausted() -> None:
    attempt_result = InvocationAttemptResult(
        result=CommandResult(returncode=0, stdout_text="retry", stderr_text=""),
        extracted_output=ExtractedInvocationOutput(
            output_text="retry",
            output_extraction_status="success",
            parsed_provider_usage=ParsedProviderUsage(status="none"),
        ),
        usage_output="retry",
    )

    transition = transition_from_structured_output(
        attempt_result=attempt_result,
        cursor=InvocationRetryCursor(
            retry_count=0,
            quota_retry_count=0,
            quota_retry_started_at=None,
        ),
        failure_retry_decision=NoFailureRetry(
            retry_count=0,
            retry_matched=True,
        ),
    )

    assert isinstance(transition, RaiseRetryExhaustedAttemptTransition)


def test_quota_failure_decision_maps_to_quota_failure_action() -> None:
    transition = transition_from_quota_retry(
        attempt_result=InvocationAttemptResult(
            result=CommandResult(returncode=0, stdout_text="quota", stderr_text=""),
            extracted_output=None,
            usage_output="quota",
        ),
        cursor=InvocationRetryCursor(
            retry_count=0,
            quota_retry_count=0,
            quota_retry_started_at=None,
        ),
        quota_retry_decision=QuotaRetryFailure(
            quota_retry_started_at=1.0,
            quota_retry_count=0,
            message="guard exceeded",
        ),
    )

    assert isinstance(transition, RaiseQuotaFailureAttemptTransition)
    assert transition.message == "guard exceeded"
    assert transition.attempt_output_for_usage == "quota"


def test_quota_schedule_decision_maps_to_sleep_action() -> None:
    notice = InvocationDiagnosticNotice(
        level="warning",
        message="quota",
        operation="quota_retry_scheduled",
    )

    transition = transition_from_quota_retry(
        attempt_result=InvocationAttemptResult(
            result=CommandResult(returncode=0, stdout_text="quota", stderr_text=""),
            extracted_output=None,
            usage_output="quota",
        ),
        cursor=InvocationRetryCursor(
            retry_count=0,
            quota_retry_count=0,
            quota_retry_started_at=None,
        ),
        quota_retry_decision=ScheduleQuotaRetry(
            quota_retry_started_at=1.0,
            quota_retry_count=1,
            wait_seconds=3,
            notice=notice,
        ),
    )

    assert isinstance(transition, SleepAndRetryAttemptTransition)
    assert transition.quota_retry_count == 1
    assert transition.notice is notice


def test_terminal_failure_prefers_failed_exit_over_retry_exhaustion() -> None:
    transition = transition_from_terminal_failure(
        attempt_result=InvocationAttemptResult(
            result=CommandResult(returncode=1, stdout_text="retry", stderr_text=""),
            extracted_output=None,
            usage_output="retry",
        ),
        cursor=InvocationRetryCursor(
            retry_count=0,
            quota_retry_count=0,
            quota_retry_started_at=None,
        ),
        retry_matched=True,
    )

    assert isinstance(transition, RaiseFailedExitAttemptTransition)
