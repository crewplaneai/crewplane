from __future__ import annotations

from orchestrator_cli.architecture.contracts import (
    InvocationContext,
    InvocationDiagnostic,
    InvocationLogLevel,
    RuntimeLogValue,
)
from orchestrator_cli.core.config import AgentConfig

from ..usage import InvocationUsage
from .state import (
    ContinueAttemptTransition,
    ExtractedInvocationOutput,
    FinalizeSuccessAttemptTransition,
    InvocationAttemptTransition,
    InvocationDiagnosticNotice,
    InvocationUsageState,
    RaiseFailedExitAttemptTransition,
    RaiseOutputExtractionFailureAttemptTransition,
    RaiseQuotaFailureAttemptTransition,
    RaiseRetryExhaustedAttemptTransition,
    SleepAndRetryAttemptTransition,
)


def emit_invocation_diagnostic(
    invocation_context: InvocationContext | None,
    level: InvocationLogLevel,
    message: str,
    operation: str,
    attributes: dict[str, RuntimeLogValue] | None = None,
) -> None:
    if invocation_context is None or invocation_context.diagnostics is None:
        return
    invocation_context.diagnostics(
        InvocationDiagnostic(
            level=level,
            message=message,
            operation=operation,
            attributes=dict(attributes) if attributes is not None else None,
        )
    )


def emit_notice(
    invocation_context: InvocationContext | None,
    notice: InvocationDiagnosticNotice | None,
) -> None:
    if notice is None:
        return
    if (
        notice.console_message is not None
        and invocation_context is not None
        and invocation_context.console_message_sink is not None
    ):
        invocation_context.console_message_sink(notice.console_message)
    emit_invocation_diagnostic(
        invocation_context,
        level=notice.level,
        message=notice.message,
        operation=notice.operation,
        attributes=notice.attributes,
    )


def record_usage(
    invocation_context: InvocationContext | None,
    usage: InvocationUsage,
) -> None:
    if invocation_context is None or invocation_context.usage_recorder is None:
        return
    invocation_context.usage_recorder(usage)


def record_usage_from_state_once(
    invocation_context: InvocationContext | None,
    config: AgentConfig,
    usage_state: InvocationUsageState,
) -> None:
    if usage_state.usage_recorded:
        return
    usage_state.usage_recorded = True
    record_usage(
        invocation_context,
        usage_state.accumulator.build_usage(
            config=config,
            output_extraction_status=usage_state.output_extraction_status,
            parsed_usage=usage_state.parsed_provider_usage,
        ),
    )


def record_transition_outputs(
    transition: InvocationAttemptTransition,
    usage_state: InvocationUsageState,
    invocation_context: InvocationContext | None,
) -> None:
    match transition:
        case FinalizeSuccessAttemptTransition(extracted_output=extracted_output):
            _record_extracted_transition_output(
                extracted_output,
                None,
                usage_state,
                invocation_context,
            )
        case (
            SleepAndRetryAttemptTransition(
                extracted_output=ExtractedInvocationOutput() as extracted_output,
                attempt_output_for_usage=attempt_output_for_usage,
            )
            | RaiseRetryExhaustedAttemptTransition(
                extracted_output=ExtractedInvocationOutput() as extracted_output,
                attempt_output_for_usage=attempt_output_for_usage,
            )
            | RaiseOutputExtractionFailureAttemptTransition(
                extracted_output=extracted_output,
                attempt_output_for_usage=attempt_output_for_usage,
            )
        ):
            _record_extracted_transition_output(
                extracted_output,
                attempt_output_for_usage,
                usage_state,
                invocation_context,
            )
        case (
            ContinueAttemptTransition(attempt_output_for_usage=attempt_output_for_usage)
            | SleepAndRetryAttemptTransition(
                attempt_output_for_usage=attempt_output_for_usage
            )
            | RaiseRetryExhaustedAttemptTransition(
                attempt_output_for_usage=attempt_output_for_usage
            )
            | RaiseFailedExitAttemptTransition(
                attempt_output_for_usage=attempt_output_for_usage
            )
            | RaiseQuotaFailureAttemptTransition(
                attempt_output_for_usage=attempt_output_for_usage
            )
        ):
            _record_attempt_output_if_present(usage_state, attempt_output_for_usage)


def _record_extracted_transition_output(
    extracted_output: ExtractedInvocationOutput,
    attempt_output_for_usage: str | None,
    usage_state: InvocationUsageState,
    invocation_context: InvocationContext | None,
) -> None:
    usage_state.record_extracted_output(extracted_output)
    emit_notice(invocation_context, extracted_output.notice)
    if (
        attempt_output_for_usage
        and extracted_output.output_path is None
        and not extracted_output.output_text
    ):
        usage_state.record_attempt_output(attempt_output_for_usage)


def _record_attempt_output_if_present(
    usage_state: InvocationUsageState,
    attempt_output_for_usage: str | None,
) -> None:
    if attempt_output_for_usage:
        usage_state.record_attempt_output(attempt_output_for_usage)
