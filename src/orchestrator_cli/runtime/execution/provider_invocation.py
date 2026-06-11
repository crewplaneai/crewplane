from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from orchestrator_cli.architecture.contracts import (
    AgentInvoker,
    InvocationContext,
    InvocationDiagnostic,
)
from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.core.config import AgentConfig
from orchestrator_cli.core.preflight.models import ProviderRecord
from orchestrator_cli.observability.timing import ElapsedTimer
from orchestrator_cli.runtime.agent.usage import (
    InvocationUsage,
    build_fallback_usage_from_output_file,
)

from .execution_activity import ExecutionTelemetry
from .execution_events import (
    InvocationEventCapture,
    InvocationMetadata,
    emit_invocation_event,
    emit_runtime_log,
    failure_event_fields,
    safe_error_message,
)
from .log_presentation import resolve_log_presentation_descriptor
from .provider_display import (
    ProviderCallDisplay,
    invoke_with_display,
    print_provider_finish,
    print_provider_start,
    provider_console_message_sink,
)
from .runtime_context import CompiledRuntimeContext


@dataclass(frozen=True)
class ProviderCallRequest:
    runtime_context: CompiledRuntimeContext
    output: ArtifactStorePort
    node_id: str
    provider: ProviderRecord
    task_id: str
    audit_round_num: int | None
    round_num: int
    prompt: str
    output_file: Path
    role_label: str
    invoker: AgentInvoker
    telemetry: ExecutionTelemetry | None
    findings_enabled: bool = False
    on_log_file_resolved: Callable[[Path], None] | None = None


@dataclass(frozen=True)
class ProviderCallResult:
    output_file: Path
    error: Exception | None = None


async def run_provider_invocation(
    request: ProviderCallRequest,
    invocation_semaphore: asyncio.Semaphore | None = None,
    capture_exception: bool = False,
    display: ProviderCallDisplay | None = None,
) -> ProviderCallResult:
    selected_display = display or ProviderCallDisplay(telemetry=request.telemetry)
    if invocation_semaphore is None:
        return await _run_provider_invocation_lifecycle(
            request,
            capture_exception,
            selected_display,
        )

    async with invocation_semaphore:
        return await _run_provider_invocation_lifecycle(
            request,
            capture_exception,
            selected_display,
        )


async def run_provider_call(
    request: ProviderCallRequest,
    display: ProviderCallDisplay | None = None,
) -> None:
    await run_provider_invocation(request, display=display)


def resolve_provider_model(
    runtime_context: CompiledRuntimeContext,
    provider: ProviderRecord,
) -> tuple[AgentConfig, str | None]:
    agent_config = runtime_context.agent_config_for_provider(provider)
    return agent_config, provider.model


async def _invoke_provider_request(
    request: ProviderCallRequest,
    display: ProviderCallDisplay,
    agent_config: AgentConfig,
    model: str | None,
    log_file: Path | None,
    invocation_context: InvocationContext,
) -> None:
    await invoke_with_display(
        display=display,
        invoker=request.invoker,
        agent_config=agent_config,
        model=model,
        prompt=request.prompt,
        output_file=request.output_file,
        log_file=log_file,
        invocation_context=invocation_context,
    )


async def _run_provider_invocation_lifecycle(
    request: ProviderCallRequest,
    capture_exception: bool,
    display: ProviderCallDisplay,
) -> ProviderCallResult:
    agent_config: AgentConfig | None = None
    invocation_metadata: InvocationMetadata | None = None
    event_capture = InvocationEventCapture()
    timer: ElapsedTimer | None = None
    try:
        agent_config, model = resolve_provider_model(
            request.runtime_context,
            request.provider,
        )
        print_provider_start(
            display,
            request.role_label,
            request.task_id,
            request.provider.provider,
            model,
        )

        invocation_metadata = InvocationMetadata(
            node_id=request.node_id,
            provider=request.provider.provider,
            role=request.role_label,
            model=model,
            task_id=request.task_id,
            audit_round_num=request.audit_round_num,
            round_num=request.round_num,
            output_file=request.output_file,
            log_file=None,
            findings_enabled=request.findings_enabled,
        )
        log_file = request.output.get_log_file(
            request.node_id,
            request.provider.provider,
            request.task_id,
            request.audit_round_num,
            request.round_num,
        )
        invocation_metadata = replace(invocation_metadata, log_file=log_file)
        descriptor = resolve_log_presentation_descriptor(
            request.invoker,
            agent_config,
            request.telemetry,
            invocation_metadata.event_context(),
        )
        if descriptor is not None:
            invocation_metadata = replace(
                invocation_metadata,
                log_presentation_format=descriptor.format,
                log_presentation_profile=descriptor.profile,
            )
        if log_file is not None and request.on_log_file_resolved is not None:
            request.on_log_file_resolved(log_file)
        invocation_context, event_capture = build_invocation_context(
            request.telemetry,
            invocation_metadata,
            display,
        )
        emit_invocation_event(
            request.telemetry,
            "invocation_started",
            invocation_metadata,
        )
        with ElapsedTimer() as timer:
            await _invoke_provider_request(
                request,
                display,
                agent_config,
                model,
                log_file,
                invocation_context,
            )
    except Exception as exc:
        _emit_invocation_failure_event(
            request,
            agent_config,
            invocation_metadata,
            event_capture,
            timer,
            exc,
        )
        if capture_exception:
            return ProviderCallResult(output_file=request.output_file, error=exc)
        raise

    return _finish_provider_invocation(
        request,
        display,
        agent_config,
        invocation_metadata,
        event_capture,
        timer,
        capture_exception,
    )


def build_invocation_context(
    telemetry: ExecutionTelemetry | None,
    metadata: InvocationMetadata,
    display: ProviderCallDisplay,
) -> tuple[InvocationContext, InvocationEventCapture]:
    capture = InvocationEventCapture()
    diagnostics = None

    def record_usage(usage: InvocationUsage) -> None:
        capture.usage = usage

    if telemetry is not None:
        event_context = metadata.event_context()

        def diagnostics_sink(diagnostic: InvocationDiagnostic) -> None:
            emit_runtime_log(
                telemetry,
                diagnostic.level,
                diagnostic.message,
                diagnostic.operation,
                context=event_context,
                attributes=diagnostic.attributes,
            )

        diagnostics = diagnostics_sink
    return (
        InvocationContext(
            node_id=metadata.node_id,
            task_id=metadata.task_id,
            provider=metadata.provider,
            role=metadata.role,
            audit_round_num=metadata.audit_round_num,
            round_num=metadata.round_num,
            findings_enabled=metadata.findings_enabled,
            diagnostics=diagnostics,
            usage_recorder=record_usage,
            console_message_sink=provider_console_message_sink(display),
        ),
        capture,
    )


def _emit_invocation_failure_event(
    request: ProviderCallRequest,
    agent_config: AgentConfig | None,
    invocation_metadata: InvocationMetadata | None,
    event_capture: InvocationEventCapture,
    timer: ElapsedTimer | None,
    exc: Exception,
) -> None:
    if agent_config is None or invocation_metadata is None:
        return

    duration_ms = timer.elapsed_milliseconds if timer is not None else None
    try:
        emit_invocation_event(
            request.telemetry,
            "invocation_failed",
            invocation_metadata,
            duration_ms=duration_ms,
            error=safe_error_message(exc),
            usage=_resolve_invocation_usage(
                capture=event_capture,
                agent_config=agent_config,
                prompt=request.prompt,
                output_file=request.output_file,
            ),
            **failure_event_fields(exc),
        )
    except Exception as telemetry_exc:
        exc.add_note(f"invocation failure telemetry failed: {telemetry_exc}")


def _finish_provider_invocation(
    request: ProviderCallRequest,
    display: ProviderCallDisplay,
    agent_config: AgentConfig | None,
    invocation_metadata: InvocationMetadata | None,
    event_capture: InvocationEventCapture,
    timer: ElapsedTimer | None,
    capture_exception: bool,
) -> ProviderCallResult:
    if agent_config is None or invocation_metadata is None:
        raise RuntimeError("Provider invocation finished without metadata.")

    duration_ms = timer.elapsed_milliseconds if timer is not None else 0
    try:
        emit_invocation_event(
            request.telemetry,
            "invocation_finished",
            invocation_metadata,
            duration_ms=duration_ms,
            usage=_resolve_invocation_usage(
                capture=event_capture,
                agent_config=agent_config,
                prompt=request.prompt,
                output_file=request.output_file,
            ),
        )
    except Exception as exc:
        if capture_exception:
            return ProviderCallResult(output_file=request.output_file, error=exc)
        raise

    print_provider_finish(display, request.task_id, request.output_file)
    return ProviderCallResult(output_file=request.output_file)


def _resolve_invocation_usage(
    capture: InvocationEventCapture,
    agent_config: AgentConfig,
    prompt: str,
    output_file: Path,
) -> InvocationUsage:
    if capture.usage is not None:
        return capture.usage
    return build_fallback_usage_from_output_file(prompt, output_file, agent_config)
