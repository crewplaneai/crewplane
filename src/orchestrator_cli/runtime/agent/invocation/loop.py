from __future__ import annotations

import asyncio
from pathlib import Path
from typing import assert_never

from orchestrator_cli.architecture.contracts import (
    CommandResult,
    CommandRunner,
    InvocationContext,
    InvocationPlan,
)
from orchestrator_cli.core.config import AgentConfig

from ..failures import (
    build_invocation_failure_error,
    build_output_extraction_failure_error,
    build_quota_failure_error,
)
from ..usage import InvocationUsageAccumulator
from .command import (
    build_invocation_runtime,
    cleanup_structured_output_file,
    prepare_runtime_for_attempt,
    run_invocation_attempt,
)
from .output import (
    build_invocation_attempt_result,
    cleanup_extracted_invocation_output,
    extract_invocation_output,
    write_extracted_invocation_output,
)
from .retry import (
    FailureRetryDecision,
    NoFailureRetry,
    ScheduleFailureRetry,
    evaluate_failure_retry,
    evaluate_quota_retry,
)
from .state import (
    ContinueAttemptTransition,
    ExtractedInvocationOutput,
    FinalizeSuccessAttemptTransition,
    InvocationAttemptResult,
    InvocationAttemptTransition,
    InvocationCommandRuntime,
    InvocationRetryCursor,
    InvocationUsageState,
    RaiseFailedExitAttemptTransition,
    RaiseOutputExtractionFailureAttemptTransition,
    RaiseQuotaFailureAttemptTransition,
    RaiseRetryExhaustedAttemptTransition,
    SleepAndRetryAttemptTransition,
)
from .telemetry import (
    emit_notice,
    record_transition_outputs,
    record_usage_from_state_once,
)
from .transitions import (
    transition_from_final_extraction,
    transition_from_quota_retry,
    transition_from_retryable_failure,
    transition_from_structured_output,
    transition_from_terminal_failure,
)


async def run_invocation_loop(
    config: AgentConfig,
    prompt: str,
    output_file: Path,
    log_file: Path | None,
    invocation_context: InvocationContext | None,
    command_runner: CommandRunner,
    plan: InvocationPlan,
) -> None:
    runtime = build_invocation_runtime(plan)
    attempt = 0
    cursor = InvocationRetryCursor(
        retry_count=0,
        quota_retry_count=0,
        quota_retry_started_at=None,
    )
    usage_state = InvocationUsageState(
        accumulator=InvocationUsageAccumulator(plan.log_provider_kind, prompt)
    )

    try:
        while True:
            prepare_runtime_for_attempt(runtime)
            usage_state.accumulator.record_attempt_start()
            result = await run_invocation_attempt(
                runtime=runtime,
                command_runner=command_runner,
                log_file=log_file,
                attempt=attempt,
                invocation_context=invocation_context,
                timeout_seconds=config.invocation_timeout_seconds,
                idle_timeout_seconds=config.invocation_idle_timeout_seconds,
            )
            attempt_result = build_invocation_attempt_result(
                runtime=runtime,
                result=result,
            )
            try:
                transition = _select_attempt_transition(
                    config=config,
                    runtime=runtime,
                    attempt_result=attempt_result,
                    cursor=cursor,
                )
                continuation = await _execute_transition_action(
                    transition=transition,
                    runtime=runtime,
                    attempt_result=attempt_result,
                    output_file=output_file,
                    log_file=log_file,
                    invocation_context=invocation_context,
                    config=config,
                    usage_state=usage_state,
                    attempt=attempt,
                )
            finally:
                result.cleanup_stream_files()
            if continuation is None:
                return
            attempt, cursor = continuation
    except asyncio.CancelledError:
        raise
    except Exception:
        record_usage_from_state_once(invocation_context, config, usage_state)
        raise
    finally:
        cleanup_structured_output_file(runtime.structured_output_file)


def _select_attempt_transition(
    config: AgentConfig,
    runtime: InvocationCommandRuntime,
    attempt_result: InvocationAttemptResult,
    cursor: InvocationRetryCursor,
) -> InvocationAttemptTransition:
    structured_retry_decision = _evaluate_structured_retry(
        config,
        runtime,
        attempt_result,
        cursor,
    )
    transition = transition_from_structured_output(
        attempt_result=attempt_result,
        cursor=cursor,
        failure_retry_decision=structured_retry_decision,
    )
    if not isinstance(transition, ContinueAttemptTransition):
        return transition
    cursor = transition.cursor()

    quota_retry_decision = evaluate_quota_retry(
        config=config,
        cmd=runtime.cmd,
        quota_parser=runtime.quota_parser,
        result=attempt_result.result,
        quota_retry_started_at=cursor.quota_retry_started_at,
        quota_retry_count=cursor.quota_retry_count,
    )
    transition = transition_from_quota_retry(
        attempt_result=attempt_result,
        cursor=cursor,
        quota_retry_decision=quota_retry_decision,
    )
    if not isinstance(transition, ContinueAttemptTransition):
        return transition
    cursor = transition.cursor()

    retry_decision = evaluate_failure_retry(
        config=config,
        cmd=runtime.cmd,
        result=attempt_result.result,
        retry_count=cursor.retry_count,
    )
    transition = transition_from_retryable_failure(
        attempt_result=attempt_result,
        cursor=cursor,
        failure_retry_decision=retry_decision,
    )
    if not isinstance(transition, ContinueAttemptTransition):
        return transition
    cursor = transition.cursor()

    transition = transition_from_terminal_failure(
        attempt_result=attempt_result,
        cursor=cursor,
        retry_matched=_retry_decision_matched(retry_decision),
    )
    if not isinstance(transition, ContinueAttemptTransition):
        return transition
    cursor = transition.cursor()

    extracted_output = extract_invocation_output(
        output_extraction_mode=runtime.output_extraction_mode,
        usage_parser=runtime.usage_parser,
        cmd=runtime.cmd,
        result=attempt_result.result,
        structured_output_file=runtime.structured_output_file,
    )
    return transition_from_final_extraction(
        attempt_result=attempt_result,
        cursor=cursor,
        extracted_output=extracted_output,
    )


def _evaluate_structured_retry(
    config: AgentConfig,
    runtime: InvocationCommandRuntime,
    attempt_result: InvocationAttemptResult,
    cursor: InvocationRetryCursor,
) -> FailureRetryDecision | None:
    if attempt_result.extracted_output is None:
        return None
    return evaluate_failure_retry(
        config=config,
        cmd=runtime.cmd,
        result=attempt_result.result,
        retry_count=cursor.retry_count,
    )


async def _execute_transition_action(
    transition: InvocationAttemptTransition,
    runtime: InvocationCommandRuntime,
    attempt_result: InvocationAttemptResult,
    output_file: Path,
    log_file: Path | None,
    invocation_context: InvocationContext | None,
    config: AgentConfig,
    usage_state: InvocationUsageState,
    attempt: int,
) -> tuple[int, InvocationRetryCursor] | None:
    try:
        match transition:
            case SleepAndRetryAttemptTransition(
                retry_delay_seconds=retry_delay_seconds
            ):
                record_transition_outputs(transition, usage_state, invocation_context)
                emit_notice(invocation_context, transition.notice)
                next_attempt = await _sleep_before_next_attempt(
                    retry_delay_seconds,
                    attempt,
                )
                return next_attempt, transition.cursor()
            case FinalizeSuccessAttemptTransition(extracted_output=extracted_output):
                record_transition_outputs(transition, usage_state, invocation_context)
                _finalize_successful_invocation(
                    output_file=output_file,
                    extracted_output=extracted_output,
                    invocation_context=invocation_context,
                    config=config,
                    usage_state=usage_state,
                )
                return None
            case RaiseRetryExhaustedAttemptTransition():
                record_transition_outputs(transition, usage_state, invocation_context)
                _raise_retry_exhausted(
                    runtime=runtime,
                    result=attempt_result.result,
                    retry_count=transition.retry_count,
                    log_file=log_file,
                )
            case RaiseFailedExitAttemptTransition():
                record_transition_outputs(transition, usage_state, invocation_context)
                _raise_failed_exit(
                    runtime=runtime,
                    result=attempt_result.result,
                    log_file=log_file,
                )
            case RaiseQuotaFailureAttemptTransition(message=message):
                record_transition_outputs(transition, usage_state, invocation_context)
                _raise_quota_failure(
                    runtime=runtime,
                    result=attempt_result.result,
                    message=message,
                )
            case RaiseOutputExtractionFailureAttemptTransition(
                extracted_output=extracted_output
            ):
                record_transition_outputs(transition, usage_state, invocation_context)
                raise build_output_extraction_failure_error(
                    runtime.cmd[0],
                    extracted_output.output_extraction_status,
                )
            case ContinueAttemptTransition():
                raise RuntimeError(
                    "Invocation loop cannot execute a continue transition."
                )
            case _:
                assert_never(transition)
    finally:
        _cleanup_transition_extracted_output(transition)


async def _sleep_before_next_attempt(wait_seconds: float, attempt: int) -> int:
    await asyncio.sleep(wait_seconds)
    return attempt + 1


def _retry_decision_matched(retry_decision: FailureRetryDecision) -> bool:
    if isinstance(retry_decision, ScheduleFailureRetry):
        return True
    if isinstance(retry_decision, NoFailureRetry):
        return retry_decision.retry_matched
    assert_never(retry_decision)


def _cleanup_transition_extracted_output(
    transition: InvocationAttemptTransition,
) -> None:
    match transition:
        case (
            FinalizeSuccessAttemptTransition(extracted_output=extracted_output)
            | SleepAndRetryAttemptTransition(extracted_output=extracted_output)
            | RaiseRetryExhaustedAttemptTransition(extracted_output=extracted_output)
            | RaiseOutputExtractionFailureAttemptTransition(
                extracted_output=extracted_output
            )
        ):
            cleanup_extracted_invocation_output(extracted_output)
        case (
            ContinueAttemptTransition()
            | RaiseFailedExitAttemptTransition()
            | RaiseQuotaFailureAttemptTransition()
        ):
            return
        case _:
            assert_never(transition)


def _finalize_successful_invocation(
    output_file: Path,
    extracted_output: ExtractedInvocationOutput,
    invocation_context: InvocationContext | None,
    config: AgentConfig,
    usage_state: InvocationUsageState,
) -> None:
    write_extracted_invocation_output(extracted_output, output_file)
    record_usage_from_state_once(invocation_context, config, usage_state)


def _raise_retry_exhausted(
    runtime: InvocationCommandRuntime,
    result: CommandResult,
    retry_count: int,
    log_file: Path | None,
) -> None:
    raise build_invocation_failure_error(
        "Command output matched configured retry conditions after "
        f"{retry_count} retries",
        runtime.failure_profile,
        result,
        log_file,
    )


def _raise_failed_exit(
    runtime: InvocationCommandRuntime,
    result: CommandResult,
    log_file: Path | None,
) -> None:
    raise build_invocation_failure_error(
        f"Exit code {result.returncode}",
        runtime.failure_profile,
        result,
        log_file,
    )


def _raise_quota_failure(
    runtime: InvocationCommandRuntime,
    result: CommandResult,
    message: str,
) -> None:
    raise build_quota_failure_error(
        message,
        runtime.failure_profile,
        result,
        None,
    )
