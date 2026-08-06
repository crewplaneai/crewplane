from __future__ import annotations

import asyncio
from enum import Enum, auto
from pathlib import Path
from typing import assert_never

from crewplane.architecture.contracts import (
    ChildProcessEnvironment,
    CommandResult,
    CommandRunner,
    InvocationContext,
    InvocationPlan,
    UsageDecodeResult,
)
from crewplane.core.config import AgentConfig

from ..failures import (
    build_invocation_failure_error,
    build_output_extraction_failure_error,
    build_quota_failure_error,
    classify_invocation_failure,
)
from ..failures.types import InvocationFailureSummary
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
    evaluate_failure_retry,
    evaluate_quota_retry,
)
from .retry_reset import reset_before_retry
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
    emit_invocation_diagnostic,
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
    cwd: Path,
    invocation_context: InvocationContext | None,
    command_runner: CommandRunner,
    plan: InvocationPlan,
    child_environment: ChildProcessEnvironment | None = None,
) -> None:
    runtime = build_invocation_runtime(plan)
    attempt = 0
    cursor = InvocationRetryCursor(
        retry_count=0,
        quota_retry_count=0,
        quota_retry_started_at=None,
    )
    quota_retry_wait_seconds = 0.0
    usage_state = InvocationUsageState(
        accumulator=InvocationUsageAccumulator(plan.log_provider_kind, prompt)
    )
    last_non_quota_failure: InvocationFailureSummary | None = None
    idle_timeout_seconds = _resolve_output_idle_timeout(
        config,
        plan,
        invocation_context,
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
                cwd=cwd,
                invocation_context=invocation_context,
                timeout_seconds=config.invocation_timeout_seconds,
                idle_timeout_seconds=idle_timeout_seconds,
                child_environment=child_environment,
            )
            usage_state.accumulator.record_provider_usage(
                _decode_provider_usage(runtime, result)
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
                    quota_retry_wait_seconds=quota_retry_wait_seconds,
                    built_in_retry_used=(
                        attempt > cursor.retry_count + cursor.quota_retry_count
                    ),
                )
                if _is_non_quota_retry(transition):
                    failure_summary = classify_invocation_failure(
                        runtime.failure_profile,
                        attempt_result.result,
                    )
                    if failure_summary.kind != "quota_or_rate_limit":
                        last_non_quota_failure = failure_summary
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
                    last_non_quota_failure=last_non_quota_failure,
                )
            finally:
                result.cleanup_stream_files()
            if continuation is None:
                return
            next_attempt, next_cursor = continuation
            if (
                isinstance(transition, SleepAndRetryAttemptTransition)
                and next_cursor.quota_retry_count > cursor.quota_retry_count
            ):
                quota_retry_wait_seconds += transition.retry_delay_seconds
            attempt, cursor = next_attempt, next_cursor
    except asyncio.CancelledError:
        raise
    except Exception:
        record_usage_from_state_once(invocation_context, config, usage_state)
        raise
    finally:
        cleanup_structured_output_file(runtime.structured_output_file)


def _resolve_output_idle_timeout(
    config: AgentConfig,
    plan: InvocationPlan,
    invocation_context: InvocationContext | None,
) -> float | None:
    configured_timeout = config.invocation_idle_timeout_seconds
    if configured_timeout is None or plan.supports_output_idle_timeout:
        return configured_timeout
    if "invocation_idle_timeout_seconds" in config.model_fields_set:
        emit_invocation_diagnostic(
            invocation_context,
            level="warning",
            message=(
                "Configured output-idle timeout cannot be enforced because this "
                "invocation emits output only after completion; continuing without an "
                "output-idle timeout. Configure invocation_timeout_seconds for a hard "
                "wall-clock limit."
            ),
            operation="invocation_idle_timeout_unavailable",
            attributes={"configured_idle_timeout_seconds": configured_timeout},
        )
    return None


def _is_non_quota_retry(transition: InvocationAttemptTransition) -> bool:
    return (
        isinstance(transition, SleepAndRetryAttemptTransition)
        and transition.notice is not None
        and transition.notice.operation != "quota_retry_scheduled"
    )


class _AttemptTransitionPhase(Enum):
    STRUCTURED_OUTPUT = auto()
    QUOTA_RETRY = auto()
    RETRYABLE_FAILURE = auto()
    TERMINAL_FAILURE = auto()


def _select_attempt_transition(
    config: AgentConfig,
    runtime: InvocationCommandRuntime,
    attempt_result: InvocationAttemptResult,
    cursor: InvocationRetryCursor,
    quota_retry_wait_seconds: float,
    built_in_retry_used: bool,
) -> InvocationAttemptTransition:
    retry_decision: FailureRetryDecision | None = None
    transition: InvocationAttemptTransition
    for phase in _AttemptTransitionPhase:
        match phase:
            case _AttemptTransitionPhase.STRUCTURED_OUTPUT:
                structured_retry_decision = _evaluate_structured_retry(
                    config,
                    runtime,
                    attempt_result,
                    cursor,
                    built_in_retry_used,
                )
                transition = transition_from_structured_output(
                    attempt_result=attempt_result,
                    cursor=cursor,
                    failure_retry_decision=structured_retry_decision,
                )
            case _AttemptTransitionPhase.QUOTA_RETRY:
                quota_retry_decision = evaluate_quota_retry(
                    config=config,
                    cmd=runtime.cmd,
                    quota_parser=runtime.quota_parser,
                    result=attempt_result.result,
                    quota_retry_started_at=cursor.quota_retry_started_at,
                    quota_retry_count=cursor.quota_retry_count,
                    quota_retry_wait_seconds=quota_retry_wait_seconds,
                    one_shot_failure_retry=runtime.one_shot_failure_retry,
                )
                transition = transition_from_quota_retry(
                    attempt_result=attempt_result,
                    cursor=cursor,
                    quota_retry_decision=quota_retry_decision,
                )
            case _AttemptTransitionPhase.RETRYABLE_FAILURE:
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
            case _AttemptTransitionPhase.TERMINAL_FAILURE:
                if retry_decision is None:
                    raise RuntimeError(
                        "Terminal failure transition requires a retry decision."
                    )
                transition = transition_from_terminal_failure(
                    attempt_result=attempt_result,
                    cursor=cursor,
                    failure_retry_decision=retry_decision,
                )
            case _:
                assert_never(phase)
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


def _evaluate_structured_retry(
    config: AgentConfig,
    runtime: InvocationCommandRuntime,
    attempt_result: InvocationAttemptResult,
    cursor: InvocationRetryCursor,
    built_in_retry_used: bool,
) -> FailureRetryDecision | None:
    if attempt_result.extracted_output is None:
        return None
    return evaluate_failure_retry(
        config=config,
        cmd=runtime.cmd,
        result=attempt_result.result,
        retry_count=cursor.retry_count,
        built_in_retry_used=built_in_retry_used,
        one_shot_failure_retry=runtime.one_shot_failure_retry,
    )


def _decode_provider_usage(
    runtime: InvocationCommandRuntime,
    result: CommandResult,
) -> UsageDecodeResult:
    if runtime.usage_decoder is None:
        return UsageDecodeResult()
    try:
        return runtime.usage_decoder(result)
    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        return UsageDecodeResult(error=f"Provider usage decoding failed: {message}")


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
    last_non_quota_failure: InvocationFailureSummary | None = None,
) -> tuple[int, InvocationRetryCursor] | None:
    try:
        if isinstance(transition, ContinueAttemptTransition):
            raise RuntimeError("Invocation loop cannot execute a continue transition.")
        record_transition_outputs(transition, usage_state, invocation_context)
        match transition:
            case SleepAndRetryAttemptTransition(
                retry_delay_seconds=retry_delay_seconds
            ):
                emit_notice(invocation_context, transition.notice)
                await reset_before_retry(invocation_context)
                next_attempt = await _sleep_before_next_attempt(
                    retry_delay_seconds,
                    attempt,
                )
                return next_attempt, transition.cursor()
            case FinalizeSuccessAttemptTransition(extracted_output=extracted_output):
                _finalize_successful_invocation(
                    output_file=output_file,
                    extracted_output=extracted_output,
                    invocation_context=invocation_context,
                    config=config,
                    usage_state=usage_state,
                )
                return None
            case RaiseRetryExhaustedAttemptTransition():
                _raise_retry_exhausted(
                    runtime=runtime,
                    result=attempt_result.result,
                    retry_count=transition.retry_count,
                    log_file=log_file,
                )
            case RaiseFailedExitAttemptTransition():
                _raise_failed_exit(
                    runtime=runtime,
                    result=attempt_result.result,
                    log_file=log_file,
                )
            case RaiseQuotaFailureAttemptTransition(message=message):
                _raise_quota_failure(
                    runtime=runtime,
                    result=attempt_result.result,
                    message=message,
                    last_non_quota_failure=last_non_quota_failure,
                )
            case RaiseOutputExtractionFailureAttemptTransition(
                extracted_output=extracted_output
            ):
                raise build_output_extraction_failure_error(
                    runtime.cmd[0],
                    extracted_output.output_extraction_status,
                )
            case _:
                assert_never(transition)
        return None
    finally:
        _cleanup_transition_extracted_output(transition)


async def _sleep_before_next_attempt(wait_seconds: float, attempt: int) -> int:
    await asyncio.sleep(wait_seconds)
    return attempt + 1


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
        f"Command output matched retry conditions after {retry_count} retries",
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
    last_non_quota_failure: InvocationFailureSummary | None = None,
) -> None:
    raise build_quota_failure_error(
        message,
        runtime.failure_profile,
        result,
        None,
        last_non_quota_failure,
    )
