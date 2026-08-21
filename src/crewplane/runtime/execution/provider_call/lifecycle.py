from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from crewplane.architecture.contracts import (
    EventType,
    InvocationContext,
    NodeArtifactRequest,
)
from crewplane.architecture.ports import ProviderProcessPublication
from crewplane.architecture.safe_files import ensure_single_link_regular_file
from crewplane.core.config import AgentConfig
from crewplane.core.preflight.models import ProviderRecord
from crewplane.observability.timing import ElapsedTimer
from crewplane.runtime.workspace import WorkspaceInvocationRequest

from ..activity.events import (
    InvocationMetadata,
    emit_invocation_event,
)
from ..log_presentation import resolve_log_presentation_descriptor
from ..runtime_context import CompiledRuntimeContext
from .artifact_capture import capture_invocation_generated_files
from .display import (
    ProviderCallDisplay,
    invoke_with_display,
    print_provider_start,
)
from .events import build_invocation_context
from .generated_files import (
    capture_generated_file_change_baseline_async,
    finalize_successful_workspace,
    rendered_workspace_file_descriptors,
)
from .lifecycle_state import ProviderInvocationLifecycleState
from .provider_output import (
    provider_output_file,
    publish_provider_output,
    validate_provider_output_file,
)
from .types import ProviderCallRequest, ProviderCallResult
from .workspace import prepare_workspace_with_cancellation


def resolve_provider_model(
    runtime_context: CompiledRuntimeContext,
    provider: ProviderRecord,
) -> tuple[AgentConfig, str | None]:
    """Resolve the configured agent and model for a compiled provider record."""

    agent_config = runtime_context.agent_config_for_provider(provider)
    return agent_config, provider.model


async def run_provider_invocation_lifecycle(
    request: ProviderCallRequest,
    capture_exception: bool,
    display: ProviderCallDisplay,
) -> ProviderCallResult:
    state = ProviderInvocationLifecycleState()
    try:
        invocation_context = _initialize_provider_invocation(request, display, state)
        invocation_context = await _prepare_provider_workspace(
            request, display, state, invocation_context
        )
        await _invoke_provider_and_finalize_workspace(
            request, display, state, invocation_context
        )
    except asyncio.CancelledError as exc:
        await state.mark_cancelled(exc)
        raise
    except Exception as exc:
        await state.record_failure(request, exc)
        if capture_exception:
            return ProviderCallResult(output_file=request.output_file, error=exc)
        raise

    return state.finish(request, display, capture_exception)


def _initialize_provider_invocation(
    request: ProviderCallRequest,
    display: ProviderCallDisplay,
    state: ProviderInvocationLifecycleState,
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
    log_file = None
    if request.output.log_cli_output:
        node = next(
            node
            for node in request.runtime_context.plan.nodes
            if node.id == request.node_id
        )
        log_file = request.output.get_node_log_file(
            NodeArtifactRequest(node.id, node.artifact_contract),
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
        requested_reasoning=request.provider.requested_reasoning,
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
    state: ProviderInvocationLifecycleState,
    invocation_context: InvocationContext,
) -> InvocationContext:
    prepared_workspace = await prepare_workspace_with_cancellation(
        _workspace_invocation_request(request),
        invocation_context,
        request.runtime_context.deferred_workspace_cleanups,
    )
    state.prepared_workspace = prepared_workspace
    state.generated_file_change_baseline = (
        await capture_generated_file_change_baseline_async(
            prepared_workspace,
            request.runtime_context.deferred_workspace_cleanups,
        )
    )
    state.invocation_metadata = state.require_invocation_metadata().with_workspace(
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
        secret_context=request.runtime_context.secret_context,
    )


def _ensure_invocation_log_file(log_file: Path | None) -> None:
    if log_file is None:
        return
    ensure_single_link_regular_file(log_file)


async def _invoke_provider_and_finalize_workspace(
    request: ProviderCallRequest,
    display: ProviderCallDisplay,
    state: ProviderInvocationLifecycleState,
    invocation_context: InvocationContext,
) -> None:
    metadata = state.require_invocation_metadata()
    prepared_workspace = state.require_prepared_workspace()
    _ensure_invocation_log_file(metadata.log_file)
    emit_invocation_event(request.telemetry, EventType.INVOCATION_STARTED, metadata)
    with ElapsedTimer() as timer:
        state.timer = timer
        await _invoke_provider_request(
            request,
            display,
            state.require_agent_config(),
            state.model,
            metadata.log_file,
            prepared_workspace.cwd,
            invocation_context,
        )
    validate_provider_output_file(request)
    if not request.defer_output_publication:
        publish_provider_output(request)
    if state.child_environment_applied:
        state.invocation_metadata = metadata.with_workspace_child_environment_applied()
    generated_file_workspace = await capture_invocation_generated_files(
        request,
        prepared_workspace,
        state.generated_file_change_baseline,
        state.require_invocation_metadata(),
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
        output_file=provider_output_file(request),
        cwd=cwd,
        log_file=log_file,
        invocation_context=invocation_context,
    )


def _rebuild_invocation_context(
    request: ProviderCallRequest,
    display: ProviderCallDisplay,
    state: ProviderInvocationLifecycleState,
) -> InvocationContext:
    def record_process_publication(publication: ProviderProcessPublication) -> None:
        if request.on_provider_process_state_published is not None:
            request.on_provider_process_state_published(publication)

    invocation_context, state.event_capture = build_invocation_context(
        request.telemetry,
        state.require_invocation_metadata(),
        display,
        request.output,
        request.runtime_context.runtime_publications,
        record_process_publication,
    )
    return invocation_context
