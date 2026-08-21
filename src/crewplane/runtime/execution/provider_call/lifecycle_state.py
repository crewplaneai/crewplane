from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

from crewplane.architecture.contracts import EventType
from crewplane.core.config import AgentConfig
from crewplane.observability.timing import ElapsedTimer
from crewplane.runtime.workspace import PreparedWorkspace
from crewplane.runtime.workspace.cleanup_notes import note_cleanup_failure

from ..activity.events import (
    InvocationEventCapture,
    InvocationMetadata,
    emit_invocation_event,
)
from .cancellation import workspace_finalization_is_deferred
from .display import ProviderCallDisplay, print_provider_finish
from .events import emit_provider_invocation_failure_event, resolve_invocation_usage
from .generated_files import GeneratedFileChangeBaseline
from .provider_output import provider_output_file
from .types import ProviderCallRequest, ProviderCallResult
from .workspace import workspace_child_environment_applied


@dataclass
class ProviderInvocationLifecycleState:
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

    def require_agent_config(self) -> AgentConfig:
        if self.agent_config is None:
            raise RuntimeError("Provider invocation started without agent config.")
        return self.agent_config

    def require_invocation_metadata(self) -> InvocationMetadata:
        if self.invocation_metadata is None:
            raise RuntimeError("Provider invocation started without metadata.")
        return self.invocation_metadata

    def require_prepared_workspace(self) -> PreparedWorkspace:
        if self.prepared_workspace is None:
            raise RuntimeError("Provider invocation started without a workspace.")
        return self.prepared_workspace

    async def mark_cancelled(self, cancellation: BaseException) -> None:
        if self.prepared_workspace is None:
            return
        if self.workspace_terminal_state_recorded or _workspace_state_is_terminal(
            self.prepared_workspace
        ):
            return
        if (
            self.workspace_success_finalization_started
            and workspace_finalization_is_deferred(cancellation)
        ):
            return
        try:
            await asyncio.to_thread(
                self.prepared_workspace.mark_cancelled,
                "Provider invocation was cancelled.",
                self.child_environment_status(),
            )
        except Exception as cleanup_error:
            note_cleanup_failure(
                cancellation,
                "Workspace cancellation handling",
                cleanup_error,
            )

    async def record_failure(
        self,
        request: ProviderCallRequest,
        failure: Exception,
    ) -> None:
        if self.invocation_metadata is not None and self.child_environment_applied:
            self.invocation_metadata = (
                self.invocation_metadata.with_workspace_child_environment_applied()
            )
        await self._mark_prepared_workspace_failed(failure)
        emit_provider_invocation_failure_event(
            request.telemetry,
            self.agent_config,
            self.invocation_metadata,
            self.event_capture,
            self.timer,
            failure,
            request.prompt,
            provider_output_file(request),
        )

    async def _mark_prepared_workspace_failed(self, failure: Exception) -> None:
        if self.prepared_workspace is None:
            return
        try:
            await asyncio.to_thread(
                self.prepared_workspace.mark_failed,
                str(failure),
                self.child_environment_status(),
            )
        except Exception as cleanup_error:
            note_cleanup_failure(
                failure,
                "Workspace failure handling",
                cleanup_error,
            )

    def finish(
        self,
        request: ProviderCallRequest,
        display: ProviderCallDisplay,
        capture_exception: bool,
    ) -> ProviderCallResult:
        if self.agent_config is None or self.invocation_metadata is None:
            raise RuntimeError("Provider invocation finished without metadata.")

        duration_ms = self.timer.elapsed_milliseconds if self.timer is not None else 0
        try:
            emit_invocation_event(
                request.telemetry,
                EventType.INVOCATION_FINISHED,
                self.invocation_metadata,
                duration_ms=duration_ms,
                usage=resolve_invocation_usage(
                    capture=self.event_capture,
                    agent_config=self.agent_config,
                    prompt=request.prompt,
                    output_file=(
                        provider_output_file(request)
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
