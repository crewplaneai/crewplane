from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from orchestrator_cli.architecture.contracts import InvocationContext
from orchestrator_cli.core.config import AgentConfig
from orchestrator_cli.core.preflight.models import ProviderRecord
from orchestrator_cli.observability.timing import ElapsedTimer
from orchestrator_cli.runtime.workspace import (
    PreparedWorkspace,
    WorkspaceInvocationRequest,
)
from orchestrator_cli.runtime.workspace.cleanup_notes import note_cleanup_failure

from .execution_events import (
    InvocationEventCapture,
    InvocationMetadata,
    emit_invocation_event,
)
from .log_presentation import resolve_log_presentation_descriptor
from .provider_display import (
    ProviderCallDisplay,
    invoke_with_display,
    print_provider_finish,
    print_provider_start,
)
from .provider_invocation_events import (
    build_invocation_context,
    emit_provider_invocation_failure_event,
    resolve_invocation_usage,
)
from .provider_invocation_generated_files import (
    finalize_successful_workspace,
    rendered_workspace_file_descriptors,
    snapshot_invocation_generated_files_async,
)
from .provider_invocation_types import ProviderCallRequest, ProviderCallResult
from .provider_invocation_workspace import (
    prepare_workspace_with_cancellation,
    workspace_child_environment_applied,
)
from .runtime_context import CompiledRuntimeContext


@dataclass
class _ProviderInvocationLifecycleState:
    agent_config: AgentConfig | None = None
    model: str | None = None
    invocation_metadata: InvocationMetadata | None = None
    event_capture: InvocationEventCapture = field(
        default_factory=InvocationEventCapture
    )
    prepared_workspace: PreparedWorkspace | None = None
    timer: ElapsedTimer | None = None
    child_environment_applied: bool = False
    workspace_success_finalization_started: bool = False
    workspace_terminal_state_recorded: bool = False

    def record_child_environment_applied(self) -> None:
        self.child_environment_applied = True

    def child_environment_status(self) -> bool | None:
        if self.prepared_workspace is None:
            return None
        return workspace_child_environment_applied(
            self.prepared_workspace,
            self.child_environment_applied,
        )


def resolve_provider_model(
    runtime_context: CompiledRuntimeContext,
    provider: ProviderRecord,
) -> tuple[AgentConfig, str | None]:
    agent_config = runtime_context.agent_config_for_provider(provider)
    return agent_config, provider.model


async def run_provider_invocation_lifecycle(
    request: ProviderCallRequest,
    capture_exception: bool,
    display: ProviderCallDisplay,
) -> ProviderCallResult:
    state = _ProviderInvocationLifecycleState()
    try:
        invocation_context = _initialize_provider_invocation(request, display, state)
        invocation_context = await _prepare_provider_workspace(
            request, display, state, invocation_context
        )
        await _invoke_provider_and_finalize_workspace(
            request, display, state, invocation_context
        )
    except asyncio.CancelledError as exc:
        await _mark_workspace_cancelled(state, exc)
        raise
    except Exception as exc:
        result = await _handle_terminal_invocation_failure(
            request, state, exc, capture_exception
        )
        if result is not None:
            return result
        raise

    return _finish_provider_invocation(
        request,
        display,
        state.agent_config,
        state.invocation_metadata,
        state.event_capture,
        state.timer,
        capture_exception,
    )


def _initialize_provider_invocation(
    request: ProviderCallRequest,
    display: ProviderCallDisplay,
    state: _ProviderInvocationLifecycleState,
) -> InvocationContext:
    agent_config, model = resolve_provider_model(
        request.runtime_context, request.provider
    )
    state.agent_config = agent_config
    state.model = model
    print_provider_start(
        display,
        request.role_label,
        request.task_id,
        request.provider.provider,
        model,
    )
    metadata = _initial_invocation_metadata(request, model)
    state.invocation_metadata = metadata
    log_file = request.output.get_log_file(
        request.node_id,
        request.provider.provider,
        request.task_id,
        request.audit_round_num,
        request.round_num,
    )
    state.invocation_metadata = _metadata_with_log_presentation(
        request,
        agent_config,
        replace(metadata, log_file=log_file),
    )
    if log_file is not None and request.on_log_file_resolved is not None:
        request.on_log_file_resolved(log_file)
    return _rebuild_invocation_context(request, display, state)


def _initial_invocation_metadata(
    request: ProviderCallRequest,
    model: str | None,
) -> InvocationMetadata:
    return InvocationMetadata(
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


def _metadata_with_log_presentation(
    request: ProviderCallRequest,
    agent_config: AgentConfig,
    metadata: InvocationMetadata,
) -> InvocationMetadata:
    descriptor = resolve_log_presentation_descriptor(
        request.invoker,
        agent_config,
        request.telemetry,
        metadata.event_context(),
    )
    if descriptor is None:
        return metadata
    return replace(
        metadata,
        log_presentation_format=descriptor.format,
        log_presentation_profile=descriptor.profile,
    )


async def _prepare_provider_workspace(
    request: ProviderCallRequest,
    display: ProviderCallDisplay,
    state: _ProviderInvocationLifecycleState,
    invocation_context: InvocationContext,
) -> InvocationContext:
    prepared_workspace = await prepare_workspace_with_cancellation(
        _workspace_invocation_request(request),
        invocation_context,
        request.runtime_context.deferred_workspace_cleanups,
    )
    state.prepared_workspace = prepared_workspace
    state.invocation_metadata = _require_invocation_metadata(state).with_workspace(
        prepared_workspace.invocation_context.workspace
    )
    invocation_context = _rebuild_invocation_context(request, display, state)
    return replace(
        invocation_context,
        workspace=prepared_workspace.invocation_context.workspace,
        retry_reset=prepared_workspace.invocation_context.retry_reset,
        workspace_environment_applied_recorder=state.record_child_environment_applied,
    )


def _workspace_invocation_request(
    request: ProviderCallRequest,
) -> WorkspaceInvocationRequest:
    return WorkspaceInvocationRequest(
        plan=request.runtime_context.plan,
        output=request.output,
        node_id=request.node_id,
        task_id=request.task_id,
        provider=request.provider.provider,
        role_label=request.role_label,
        round_num=request.round_num,
        audit_round_num=request.audit_round_num,
        materialization_limiter=request.runtime_context.workspace_materialization_limiter,
        worktree_reuse_cache=request.runtime_context.worktree_reuse_cache,
        rendered_workspace_files=rendered_workspace_file_descriptors(request),
    )


async def _invoke_provider_and_finalize_workspace(
    request: ProviderCallRequest,
    display: ProviderCallDisplay,
    state: _ProviderInvocationLifecycleState,
    invocation_context: InvocationContext,
) -> None:
    metadata = _require_invocation_metadata(state)
    prepared_workspace = _require_prepared_workspace(state)
    emit_invocation_event(request.telemetry, "invocation_started", metadata)
    with ElapsedTimer() as timer:
        state.timer = timer
        await _invoke_provider_request(
            request,
            display,
            _require_agent_config(state),
            state.model,
            metadata.log_file,
            prepared_workspace.cwd,
            invocation_context,
        )
    if state.child_environment_applied:
        state.invocation_metadata = metadata.with_workspace_child_environment_applied()
    generated_file_workspace = await snapshot_invocation_generated_files_async(
        request,
        prepared_workspace,
    )
    state.workspace_success_finalization_started = True
    await finalize_successful_workspace(
        request,
        prepared_workspace,
        state.child_environment_status(),
        generated_file_workspace,
    )
    state.workspace_terminal_state_recorded = True


async def _invoke_provider_request(
    request: ProviderCallRequest,
    display: ProviderCallDisplay,
    agent_config: AgentConfig,
    model: str | None,
    log_file: Path | None,
    cwd: Path,
    invocation_context: InvocationContext,
) -> None:
    await invoke_with_display(
        display=display,
        invoker=request.invoker,
        agent_config=agent_config,
        model=model,
        prompt=request.prompt,
        output_file=request.output_file,
        cwd=cwd,
        log_file=log_file,
        invocation_context=invocation_context,
    )


async def _mark_workspace_cancelled(
    state: _ProviderInvocationLifecycleState,
    cancellation: BaseException,
) -> None:
    if state.prepared_workspace is None:
        return
    if state.workspace_terminal_state_recorded or _workspace_state_is_terminal(
        state.prepared_workspace
    ):
        return
    if state.workspace_success_finalization_started and getattr(
        cancellation,
        "_orchestrator_workspace_finalization_deferred",
        False,
    ):
        return
    try:
        await asyncio.to_thread(
            state.prepared_workspace.mark_cancelled,
            "Provider invocation was cancelled.",
            state.child_environment_status(),
        )
    except Exception as cleanup_error:
        note_cleanup_failure(
            cancellation,
            "Workspace cancellation handling",
            cleanup_error,
        )


def _workspace_state_is_terminal(prepared_workspace: PreparedWorkspace) -> bool:
    state_path = prepared_workspace.state_path
    if state_path is None or not state_path.is_file() or state_path.is_symlink():
        return False
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    return payload.get("status") in {"succeeded", "failed", "cancelled"}


async def _handle_terminal_invocation_failure(
    request: ProviderCallRequest,
    state: _ProviderInvocationLifecycleState,
    exc: Exception,
    capture_exception: bool,
) -> ProviderCallResult | None:
    if state.invocation_metadata is not None and state.child_environment_applied:
        state.invocation_metadata = (
            state.invocation_metadata.with_workspace_child_environment_applied()
        )
    if state.prepared_workspace is not None:
        try:
            await asyncio.to_thread(
                state.prepared_workspace.mark_failed,
                str(exc),
                state.child_environment_status(),
            )
        except Exception as cleanup_error:
            note_cleanup_failure(
                exc,
                "Workspace failure handling",
                cleanup_error,
            )
    emit_provider_invocation_failure_event(
        request.telemetry,
        state.agent_config,
        state.invocation_metadata,
        state.event_capture,
        state.timer,
        exc,
        request.prompt,
        request.output_file,
    )
    if capture_exception:
        return ProviderCallResult(output_file=request.output_file, error=exc)
    return None


def _rebuild_invocation_context(
    request: ProviderCallRequest,
    display: ProviderCallDisplay,
    state: _ProviderInvocationLifecycleState,
) -> InvocationContext:
    invocation_context, state.event_capture = build_invocation_context(
        request.telemetry,
        _require_invocation_metadata(state),
        display,
    )
    return invocation_context


def _require_agent_config(
    state: _ProviderInvocationLifecycleState,
) -> AgentConfig:
    if state.agent_config is None:
        raise RuntimeError("Provider invocation started without agent config.")
    return state.agent_config


def _require_invocation_metadata(
    state: _ProviderInvocationLifecycleState,
) -> InvocationMetadata:
    if state.invocation_metadata is None:
        raise RuntimeError("Provider invocation started without metadata.")
    return state.invocation_metadata


def _require_prepared_workspace(
    state: _ProviderInvocationLifecycleState,
) -> PreparedWorkspace:
    if state.prepared_workspace is None:
        raise RuntimeError("Provider invocation started without a workspace.")
    return state.prepared_workspace


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
            usage=resolve_invocation_usage(
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
