from __future__ import annotations

from crewplane.core.config import AgentConfig

from .output import extract_invocation_output
from .retry import FailureRetryDecision, evaluate_failure_retry, evaluate_quota_retry
from .state import (
    ContinueAttemptTransition,
    InvocationAttemptResult,
    InvocationAttemptTransition,
    InvocationCommandRuntime,
    InvocationRetryCursor,
)
from .transitions import (
    transition_from_final_extraction,
    transition_from_quota_retry,
    transition_from_retryable_failure,
    transition_from_structured_output,
    transition_from_terminal_failure,
)


def select_attempt_transition(
    config: AgentConfig,
    runtime: InvocationCommandRuntime,
    attempt_result: InvocationAttemptResult,
    cursor: InvocationRetryCursor,
    quota_retry_wait_seconds: float,
    built_in_retry_used: bool,
) -> InvocationAttemptTransition:
    transition = _select_structured_output_transition(
        config,
        runtime,
        attempt_result,
        cursor,
        built_in_retry_used,
    )
    if not isinstance(transition, ContinueAttemptTransition):
        return transition
    cursor = transition.cursor()

    transition = _select_quota_transition(
        config,
        runtime,
        attempt_result,
        cursor,
        quota_retry_wait_seconds,
    )
    if not isinstance(transition, ContinueAttemptTransition):
        return transition
    cursor = transition.cursor()

    transition = _select_failure_transition(
        config,
        runtime,
        attempt_result,
        cursor,
        built_in_retry_used,
    )
    if not isinstance(transition, ContinueAttemptTransition):
        return transition
    cursor = transition.cursor()

    extracted_output = extract_invocation_output(
        output_extractor=runtime.output_extractor,
        cmd=runtime.cmd,
        result=attempt_result.result,
        structured_output_file=runtime.structured_output_file,
    )
    return transition_from_final_extraction(
        attempt_result=attempt_result,
        cursor=cursor,
        extracted_output=extracted_output,
    )


def _select_structured_output_transition(
    config: AgentConfig,
    runtime: InvocationCommandRuntime,
    attempt_result: InvocationAttemptResult,
    cursor: InvocationRetryCursor,
    built_in_retry_used: bool,
) -> InvocationAttemptTransition:
    retry_decision: FailureRetryDecision | None = None
    if attempt_result.extracted_output is not None:
        retry_decision = evaluate_failure_retry(
            config=config,
            cmd=runtime.cmd,
            result=attempt_result.result,
            retry_count=cursor.retry_count,
            built_in_retry_used=built_in_retry_used,
            one_shot_failure_retry=runtime.one_shot_failure_retry,
        )
    return transition_from_structured_output(
        attempt_result=attempt_result,
        cursor=cursor,
        failure_retry_decision=retry_decision,
    )


def _select_quota_transition(
    config: AgentConfig,
    runtime: InvocationCommandRuntime,
    attempt_result: InvocationAttemptResult,
    cursor: InvocationRetryCursor,
    quota_retry_wait_seconds: float,
) -> InvocationAttemptTransition:
    retry_decision = evaluate_quota_retry(
        config=config,
        cmd=runtime.cmd,
        quota_classifier=runtime.quota_classifier,
        result=attempt_result.result,
        quota_retry_started_at=cursor.quota_retry_started_at,
        quota_retry_count=cursor.quota_retry_count,
        quota_retry_wait_seconds=quota_retry_wait_seconds,
        one_shot_failure_retry=runtime.one_shot_failure_retry,
    )
    return transition_from_quota_retry(
        attempt_result=attempt_result,
        cursor=cursor,
        quota_retry_decision=retry_decision,
    )


def _select_failure_transition(
    config: AgentConfig,
    runtime: InvocationCommandRuntime,
    attempt_result: InvocationAttemptResult,
    cursor: InvocationRetryCursor,
    built_in_retry_used: bool,
) -> InvocationAttemptTransition:
    retry_decision = evaluate_failure_retry(
        config=config,
        cmd=runtime.cmd,
        result=attempt_result.result,
        retry_count=cursor.retry_count,
        built_in_retry_used=built_in_retry_used,
        one_shot_failure_retry=runtime.one_shot_failure_retry,
    )
    transition = transition_from_retryable_failure(
        attempt_result=attempt_result,
        cursor=cursor,
        failure_retry_decision=retry_decision,
    )
    if not isinstance(transition, ContinueAttemptTransition):
        return transition
    return transition_from_terminal_failure(
        attempt_result=attempt_result,
        cursor=transition.cursor(),
        failure_retry_decision=retry_decision,
    )
