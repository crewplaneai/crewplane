from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Never, assert_never

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


@dataclass(frozen=True)
class _InvocationLoopContext:
    config: AgentConfig
    runtime: InvocationCommandRuntime
    output_file: Path
    log_file: Path | None
    cwd: Path
    invocation_context: InvocationContext | None
    command_runner: CommandRunner
    idle_timeout_seconds: float | None
    child_environment: ChildProcessEnvironment | None


@dataclass
class _InvocationLoopState:
    usage_state: InvocationUsageState
    cursor: InvocationRetryCursor
    attempt: int = 0
    quota_retry_wait_seconds: float = 0.0
    last_non_quota_failure: InvocationFailureSummary | None = None

    @property
    def built_in_retry_used(self) -> bool:
        return self.attempt > self.cursor.retry_count + self.cursor.quota_retry_count


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
    """Run provider attempts through a terminal outcome and release their resources."""

    runtime = build_invocation_runtime(plan)
    state = _InvocationLoopState(
        usage_state=InvocationUsageState(
            accumulator=InvocationUsageAccumulator(plan.log_provider_kind, prompt)
        ),
        cursor=InvocationRetryCursor(
            retry_count=0,
            quota_retry_count=0,
            quota_retry_started_at=None,
        ),
    )
    context = _InvocationLoopContext(
        config=config,
        runtime=runtime,
        output_file=output_file,
        log_file=log_file,
        cwd=cwd,
        invocation_context=invocation_context,
        command_runner=command_runner,
        idle_timeout_seconds=_resolve_output_idle_timeout(
            config, plan, invocation_context
        ),
        child_environment=child_environment,
    )

    try:
        while True:
            retry = await _run_attempt_cycle(context, state)
            if retry is None:
                return
            _advance_retry_state(state, retry)
    except asyncio.CancelledError:
        raise
    except Exception:
        record_usage_from_state_once(invocation_context, config, state.usage_state)
        raise
    finally:
        cleanup_structured_output_file(runtime.structured_output_file)


async def _run_attempt_cycle(
    context: _InvocationLoopContext,
    state: _InvocationLoopState,
) -> SleepAndRetryAttemptTransition | None:
    result = await _run_attempt_command(context, state)
    try:
        state.usage_state.accumulator.record_provider_usage(
            _decode_provider_usage(context.runtime, result)
        )
        attempt_result = build_invocation_attempt_result(context.runtime, result)
        transition = _select_attempt_transition(
            config=context.config,
            runtime=context.runtime,
            attempt_result=attempt_result,
            cursor=state.cursor,
            quota_retry_wait_seconds=state.quota_retry_wait_seconds,
            built_in_retry_used=state.built_in_retry_used,
        )
        _remember_non_quota_failure(context.runtime, state, attempt_result, transition)
        return await _execute_transition_action(
            transition, context, attempt_result, state
        )
    finally:
        result.cleanup_stream_files()


async def _run_attempt_command(
    context: _InvocationLoopContext,
    state: _InvocationLoopState,
) -> CommandResult:
    prepare_runtime_for_attempt(context.runtime)
    state.usage_state.accumulator.record_attempt_start()
    return await run_invocation_attempt(
        runtime=context.runtime,
        command_runner=context.command_runner,
        log_file=context.log_file,
        attempt=state.attempt,
        cwd=context.cwd,
        invocation_context=context.invocation_context,
        timeout_seconds=context.config.invocation_timeout_seconds,
        idle_timeout_seconds=context.idle_timeout_seconds,
        child_environment=context.child_environment,
    )


def _remember_non_quota_failure(
    runtime: InvocationCommandRuntime,
    state: _InvocationLoopState,
    attempt_result: InvocationAttemptResult,
    transition: InvocationAttemptTransition,
) -> None:
    if not _is_non_quota_retry(transition):
        return
    failure_summary = classify_invocation_failure(
        runtime.failure_profile, attempt_result.result
    )
    if failure_summary.kind != "quota_or_rate_limit":
        state.last_non_quota_failure = failure_summary


def _is_non_quota_retry(transition: InvocationAttemptTransition) -> bool:
    return (
        isinstance(transition, SleepAndRetryAttemptTransition)
        and transition.notice is not None
        and transition.notice.operation != "quota_retry_scheduled"
    )


def _advance_retry_state(
    state: _InvocationLoopState,
    retry: SleepAndRetryAttemptTransition,
) -> None:
    next_cursor = retry.cursor()
    if next_cursor.quota_retry_count > state.cursor.quota_retry_count:
        state.quota_retry_wait_seconds += retry.retry_delay_seconds
    state.attempt += 1
    state.cursor = next_cursor


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


def _select_attempt_transition(
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
        quota_parser=runtime.quota_parser,
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
    context: _InvocationLoopContext,
    attempt_result: InvocationAttemptResult,
    state: _InvocationLoopState,
) -> SleepAndRetryAttemptTransition | None:
    runtime = context.runtime
    invocation_context = context.invocation_context
    try:
        if isinstance(transition, ContinueAttemptTransition):
            raise RuntimeError("Invocation loop cannot execute a continue transition.")
        record_transition_outputs(transition, state.usage_state, invocation_context)
        match transition:
            case SleepAndRetryAttemptTransition(
                retry_delay_seconds=retry_delay_seconds
            ):
                emit_notice(invocation_context, transition.notice)
                await reset_before_retry(invocation_context)
                await asyncio.sleep(retry_delay_seconds)
                return transition
            case FinalizeSuccessAttemptTransition(extracted_output=extracted_output):
                _finalize_successful_invocation(
                    output_file=context.output_file,
                    extracted_output=extracted_output,
                    invocation_context=invocation_context,
                    config=context.config,
                    usage_state=state.usage_state,
                )
                return None
            case RaiseRetryExhaustedAttemptTransition():
                _raise_retry_exhausted(
                    runtime=runtime,
                    result=attempt_result.result,
                    retry_count=transition.retry_count,
                    log_file=context.log_file,
                )
            case RaiseFailedExitAttemptTransition():
                _raise_failed_exit(
                    runtime=runtime,
                    result=attempt_result.result,
                    log_file=context.log_file,
                )
            case RaiseQuotaFailureAttemptTransition(message=message):
                _raise_quota_failure(
                    runtime=runtime,
                    result=attempt_result.result,
                    message=message,
                    last_non_quota_failure=state.last_non_quota_failure,
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
    finally:
        _cleanup_transition_extracted_output(transition)


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
) -> Never:
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
) -> Never:
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
) -> Never:
    raise build_quota_failure_error(
        message,
        runtime.failure_profile,
        result,
        None,
        last_non_quota_failure,
    )
