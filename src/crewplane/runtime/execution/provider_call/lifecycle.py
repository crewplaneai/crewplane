from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

from crewplane.architecture.contracts import InvocationContext, NodeArtifactRequest
from crewplane.architecture.ports import ProviderProcessPublication
from crewplane.architecture.safe_files import (
    ensure_single_link_regular_file,
    replace_contained_file,
)
from crewplane.core.config import AgentConfig
from crewplane.core.preflight.models import ProviderRecord
from crewplane.observability.timing import ElapsedTimer
from crewplane.runtime.workspace import (
    PreparedWorkspace,
    WorkspaceInvocationRequest,
)
from crewplane.runtime.workspace.cleanup_notes import note_cleanup_failure

from ..activity.events import (
    InvocationEventCapture,
    InvocationMetadata,
    emit_invocation_event,
)
from ..log_presentation import resolve_log_presentation_descriptor
from ..publication_registry import RuntimePublicationRegistry
from ..runtime_context import CompiledRuntimeContext
from .artifact_capture import capture_invocation_generated_files
from .cancellation import workspace_finalization_is_deferred
from .display import (
    ProviderCallDisplay,
    invoke_with_display,
    print_provider_finish,
    print_provider_start,
)
from .events import (
    build_invocation_context,
    emit_provider_invocation_failure_event,
    resolve_invocation_usage,
)
from .generated_files import (
    GeneratedFileChangeBaseline,
    capture_generated_file_change_baseline_async,
    finalize_successful_workspace,
    rendered_workspace_file_descriptors,
)
from .types import ProviderCallRequest, ProviderCallResult, ProviderOutputPolicy
from .workspace import (
    prepare_workspace_with_cancellation,
    workspace_child_environment_applied,
)


@dataclass
class _ProviderInvocationLifecycleState:
    agent_config: AgentConfig | None = None
    model: str | None = None
    invocation_metadata: InvocationMetadata | None = None
    event_capture: InvocationEventCapture = field(
        default_factory=InvocationEventCapture
    )
    prepared_workspace: PreparedWorkspace | None = None
    generated_file_change_baseline: GeneratedFileChangeBaseline | None = None
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


class _BinaryWriter(Protocol):
    def write(self, payload: bytes) -> int: ...


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
    state: _ProviderInvocationLifecycleState,
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
        secret_context=request.runtime_context.secret_context,
    )


def _ensure_invocation_log_file(log_file: Path | None) -> None:
    if log_file is None:
        return
    ensure_single_link_regular_file(log_file)


async def _invoke_provider_and_finalize_workspace(
    request: ProviderCallRequest,
    display: ProviderCallDisplay,
    state: _ProviderInvocationLifecycleState,
    invocation_context: InvocationContext,
) -> None:
    metadata = _require_invocation_metadata(state)
    prepared_workspace = _require_prepared_workspace(state)
    _ensure_invocation_log_file(metadata.log_file)
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
    _validate_provider_output_file(request)
    if not request.defer_output_publication:
        _publish_provider_output(request)
    if state.child_environment_applied:
        state.invocation_metadata = metadata.with_workspace_child_environment_applied()
    generated_file_workspace = await capture_invocation_generated_files(
        request,
        prepared_workspace,
        state.generated_file_change_baseline,
        _require_invocation_metadata(state),
    )
    state.workspace_success_finalization_started = True
    await finalize_successful_workspace(
        request,
        prepared_workspace,
        state.child_environment_status(),
        generated_file_workspace,
    )
    state.workspace_terminal_state_recorded = True


def _validate_provider_output_file(request: ProviderCallRequest) -> None:
    provider_output = _provider_output_file(request)
    if _is_publishable_regular_file(provider_output):
        return
    if request.provider_output_policy == ProviderOutputPolicy.ALLOW_MISSING_OUTPUT:
        return
    raise RuntimeError(
        "Provider invocation completed without the expected output file for "
        f"node '{request.node_id}' task '{request.task_id}': "
        f"{provider_output.as_posix()}"
    )


def _is_publishable_regular_file(path: Path) -> bool:
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RuntimeError(
            f"Provider output could not be inspected safely: {path.as_posix()}"
        ) from exc
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
        raise RuntimeError(
            f"Provider output must be a single-link regular file: {path.as_posix()}"
        )
    return True


def _publish_provider_output(request: ProviderCallRequest) -> None:
    provider_output = _provider_output_file(request)
    if not _is_publishable_regular_file(provider_output):
        return
    publication_signature = publish_invocation_output(
        provider_output,
        request.output_file,
        request.runtime_context.runtime_publications,
    )
    if request.on_invocation_output_published is not None:
        request.on_invocation_output_published(
            request.output_file,
            publication_signature,
        )


def publish_invocation_output(
    invocation_output_file: Path,
    output_file: Path,
    publications: RuntimePublicationRegistry,
    expected_signature: tuple[int, str] | None = None,
) -> tuple[int, str]:
    """Atomically publish trusted invocation bytes to an unoccupied output path."""

    bound_signature = expected_signature or bind_invocation_output(
        invocation_output_file
    )
    with publications.transaction():
        if invocation_output_file != output_file:
            staged_output = _stage_verified_invocation_output(
                invocation_output_file,
                output_file.parent,
                bound_signature,
            )
            try:
                replace_contained_file(
                    output_file.parent,
                    output_file.name,
                    staged_output,
                )
            except (OSError, ValueError) as exc:
                raise RuntimeError(
                    "Refusing to publish an invocation output through an unsafe "
                    f"destination: {output_file.as_posix()}"
                ) from exc
            finally:
                staged_output.unlink(missing_ok=True)
        publication_signature = bind_invocation_output(output_file)
        if publication_signature != bound_signature:
            raise RuntimeError(
                "Published invocation output does not match its bound bytes: "
                f"{output_file.as_posix()}"
            )
        publications.publish(
            output_file,
            bound_signature,
            recovery_source=output_file,
        )
    return bound_signature


def bind_invocation_output(path: Path) -> tuple[int, str]:
    """Bind one stable single-link output file to its exact byte signature."""

    descriptor, initial_stat = _open_invocation_output(path)
    try:
        size_bytes, sha256 = _hash_descriptor(descriptor)
        _ensure_open_output_unchanged(path, descriptor, initial_stat, size_bytes)
    finally:
        os.close(descriptor)
    return size_bytes, sha256


def read_bound_invocation_output(
    path: Path,
    expected_signature: tuple[int, str],
) -> str:
    """Read exact UTF-8 output bytes only when they match a prior binding."""

    payload = _read_bound_invocation_payload(path, expected_signature)
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(
            f"Invocation output is not valid UTF-8: {path.as_posix()}"
        ) from exc


def _read_bound_invocation_payload(
    path: Path,
    expected_signature: tuple[int, str],
) -> bytes:
    descriptor, initial_stat = _open_invocation_output(path)
    try:
        payload, actual_signature = _read_descriptor(descriptor)
        _ensure_open_output_unchanged(
            path,
            descriptor,
            initial_stat,
            actual_signature[0],
        )
    finally:
        os.close(descriptor)
    if actual_signature != expected_signature:
        raise RuntimeError(
            f"Invocation output does not match its bound bytes: {path.as_posix()}"
        )
    return payload


def _stage_verified_invocation_output(
    source: Path,
    destination_dir: Path,
    expected_signature: tuple[int, str],
) -> Path:
    destination_stat = _real_directory_stat(destination_dir)
    temporary_path: Path | None = None
    try:
        descriptor, initial_stat = _open_invocation_output(source)
        try:
            with tempfile.NamedTemporaryFile(
                dir=destination_dir,
                prefix=".crewplane-publication-",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                actual_signature = _copy_descriptor(descriptor, temporary)
                temporary.flush()
                os.fsync(temporary.fileno())
            _ensure_open_output_unchanged(
                source,
                descriptor,
                initial_stat,
                actual_signature[0],
            )
        finally:
            os.close(descriptor)
        if actual_signature != expected_signature:
            raise RuntimeError(
                "Invocation output changed after its bytes were bound: "
                f"{source.as_posix()}"
            )
        if not _same_directory_identity(
            destination_stat,
            _real_directory_stat(destination_dir),
        ):
            raise RuntimeError(
                "Invocation output destination changed during publication: "
                f"{destination_dir.as_posix()}"
            )
        return temporary_path
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _open_invocation_output(path: Path) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError(
            f"Invocation output is unavailable or unsafe: {path.as_posix()}"
        ) from exc
    file_stat = os.fstat(descriptor)
    if not _is_single_link_regular_file(file_stat):
        os.close(descriptor)
        raise RuntimeError(
            f"Invocation output must be a single-link regular file: {path.as_posix()}"
        )
    return descriptor, file_stat


def _ensure_open_output_unchanged(
    path: Path,
    descriptor: int,
    initial_stat: os.stat_result,
    bytes_read: int,
) -> None:
    final_stat = os.fstat(descriptor)
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise RuntimeError(
            f"Invocation output changed while being read: {path.as_posix()}"
        ) from exc
    if (
        not _same_file_identity(initial_stat, final_stat)
        or not _same_file_identity(final_stat, path_stat)
        or bytes_read != final_stat.st_size
    ):
        raise RuntimeError(
            f"Invocation output changed while being read: {path.as_posix()}"
        )


def _hash_descriptor(descriptor: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    size_bytes = 0
    while chunk := os.read(descriptor, 1024 * 1024):
        size_bytes += len(chunk)
        digest.update(chunk)
    return size_bytes, digest.hexdigest()


def _read_descriptor(descriptor: int) -> tuple[bytes, tuple[int, str]]:
    digest = hashlib.sha256()
    payload = bytearray()
    while chunk := os.read(descriptor, 1024 * 1024):
        payload.extend(chunk)
        digest.update(chunk)
    return bytes(payload), (len(payload), digest.hexdigest())


def _copy_descriptor(descriptor: int, destination: _BinaryWriter) -> tuple[int, str]:
    digest = hashlib.sha256()
    size_bytes = 0
    while chunk := os.read(descriptor, 1024 * 1024):
        destination.write(chunk)
        size_bytes += len(chunk)
        digest.update(chunk)
    return size_bytes, digest.hexdigest()


def _same_file_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        _is_single_link_regular_file(second)
        and first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
        and first.st_ctime_ns == second.st_ctime_ns
    )


def _is_single_link_regular_file(file_stat: os.stat_result) -> bool:
    return stat.S_ISREG(file_stat.st_mode) and file_stat.st_nlink == 1


def _same_directory_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(second.st_mode)
        and not stat.S_ISLNK(second.st_mode)
        and first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
    )


def _real_directory_stat(path: Path) -> os.stat_result:
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise RuntimeError(
            f"Invocation output destination is unavailable: {path.as_posix()}"
        ) from exc
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
        raise RuntimeError(
            f"Invocation output destination must be a real directory: {path.as_posix()}"
        )
    return path_stat


def _provider_output_file(request: ProviderCallRequest) -> Path:
    return request.invocation_output_file or request.output_file


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
        output_file=_provider_output_file(request),
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
    if (
        state.workspace_success_finalization_started
        and workspace_finalization_is_deferred(cancellation)
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
        _provider_output_file(request),
    )
    if capture_exception:
        return ProviderCallResult(output_file=request.output_file, error=exc)
    return None


def _rebuild_invocation_context(
    request: ProviderCallRequest,
    display: ProviderCallDisplay,
    state: _ProviderInvocationLifecycleState,
) -> InvocationContext:
    def record_process_publication(publication: ProviderProcessPublication) -> None:
        if request.on_provider_process_state_published is not None:
            request.on_provider_process_state_published(publication)

    invocation_context, state.event_capture = build_invocation_context(
        request.telemetry,
        _require_invocation_metadata(state),
        display,
        request.output,
        request.runtime_context.runtime_publications,
        record_process_publication,
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
                output_file=(
                    _provider_output_file(request)
                    if request.defer_output_publication
                    else request.output_file
                ),
            ),
        )
    except Exception as exc:
        if capture_exception:
            return ProviderCallResult(output_file=request.output_file, error=exc)
        raise

    print_provider_finish(display, request.task_id, request.output_file)
    return ProviderCallResult(output_file=request.output_file)
