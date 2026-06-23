from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from crewplane.architecture.contracts import InvocationWorkspaceContext
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.observability.events import (
    ExecutionEventContext,
    InvocationEventType,
    LogLevel,
    NodeEventType,
    RuntimeLogValue,
    WorkflowEventType,
    emit_event,
    invocation_event,
    node_event,
    runtime_log_event,
    workflow_event,
    workspace_event,
)
from crewplane.observability.events.payloads import WorkspaceEventPayload
from crewplane.runtime.agent.failures import InvocationFailureError
from crewplane.runtime.agent.usage import InvocationUsage

from .telemetry import ExecutionTelemetry

INVOCATION_WORKSPACE_STATUSES = {
    "invocation_started": "running",
    "invocation_finished": "succeeded",
    "invocation_failed": "failed",
}


@dataclass(frozen=True)
class RuntimeEventContext:
    node_id: str | None = None
    provider: str | None = None
    role: ProviderRole | None = None
    model: str | None = None
    task_id: str | None = None
    audit_round_num: int | None = None
    round_num: int | None = None
    output_file: Path | None = None
    log_file: Path | None = None
    log_presentation_format: str | None = None
    log_presentation_profile: str | None = None

    def as_execution_event_context(
        self,
        workflow_name: str,
        run_id: str,
    ) -> ExecutionEventContext:
        return ExecutionEventContext(
            workflow_name=workflow_name,
            run_id=run_id,
            node_id=self.node_id,
            provider=self.provider,
            role=self.role,
            model=self.model,
            task_id=self.task_id,
            audit_round_num=self.audit_round_num,
            round_num=self.round_num,
            output_file=str(self.output_file) if self.output_file else None,
            log_file=str(self.log_file) if self.log_file else None,
            log_presentation_format=self.log_presentation_format,
            log_presentation_profile=self.log_presentation_profile,
        )


@dataclass(frozen=True)
class InvocationWorkspaceMetadata:
    workspace_kind: str | None = None
    logical_worktree_name: str | None = None
    materialization: str | None = None
    source_kind: str | None = None
    source_node_id: str | None = None
    source_commit: str | None = None
    source_tree: str | None = None
    worktree_contract_mode: str | None = None
    worktree_contract_schema_version: str | None = None
    state_path: Path | None = None
    writable: bool | None = None
    lineage_producer: bool | None = None
    child_environment_required: bool | None = None
    child_environment_applied: bool | None = None

    @classmethod
    def from_context(
        cls,
        workspace: InvocationWorkspaceContext,
    ) -> InvocationWorkspaceMetadata:
        source = workspace.invocation_source
        return cls(
            workspace_kind=workspace.workspace_kind,
            logical_worktree_name=workspace.logical_worktree_name,
            materialization=workspace.materialization,
            source_kind=source.source_kind,
            source_node_id=source.source_node_id,
            source_commit=source.source_commit,
            source_tree=source.source_tree,
            worktree_contract_mode=workspace.worktree_contract.mode,
            worktree_contract_schema_version=workspace.worktree_contract.schema_version,
            state_path=workspace.workspace_state_path,
            writable=workspace.writable,
            lineage_producer=workspace.lineage_producer,
            child_environment_required=workspace.child_environment_required,
            child_environment_applied=workspace.child_environment_applied,
        )

    def event_payload(self, status: str | None) -> WorkspaceEventPayload:
        return WorkspaceEventPayload(
            status=status,
            workspace_kind=self.workspace_kind,
            workspace_logical_worktree_name=self.logical_worktree_name,
            workspace_materialization=self.materialization,
            workspace_source_kind=self.source_kind,
            workspace_source_node_id=self.source_node_id,
            workspace_source_commit=self.source_commit,
            workspace_source_tree=self.source_tree,
            worktree_contract_mode=self.worktree_contract_mode,
            worktree_contract_schema_version=self.worktree_contract_schema_version,
            workspace_state_path=str(self.state_path) if self.state_path else None,
            workspace_writable=self.writable,
            workspace_lineage_producer=self.lineage_producer,
            workspace_child_environment_required=self.child_environment_required,
            workspace_child_environment_applied=self.child_environment_applied,
        )


@dataclass(frozen=True)
class InvocationMetadata:
    node_id: str
    provider: str
    role: ProviderRole
    model: str | None
    task_id: str
    audit_round_num: int | None
    round_num: int
    output_file: Path
    log_file: Path | None
    findings_enabled: bool = False
    log_presentation_format: str | None = None
    log_presentation_profile: str | None = None
    workspace: InvocationWorkspaceMetadata | None = None

    def event_context(self) -> RuntimeEventContext:
        return RuntimeEventContext(
            node_id=self.node_id,
            provider=self.provider,
            role=self.role,
            model=self.model,
            task_id=self.task_id,
            audit_round_num=self.audit_round_num,
            round_num=self.round_num,
            output_file=self.output_file,
            log_file=self.log_file,
            log_presentation_format=self.log_presentation_format,
            log_presentation_profile=self.log_presentation_profile,
        )

    def with_workspace(
        self,
        workspace: InvocationWorkspaceContext | None,
    ) -> InvocationMetadata:
        if workspace is None:
            return self
        return replace(
            self,
            workspace=InvocationWorkspaceMetadata.from_context(workspace),
        )

    def with_workspace_child_environment_applied(self) -> InvocationMetadata:
        if (
            self.workspace is None
            or self.workspace.child_environment_required is not True
        ):
            return self
        return replace(
            self,
            workspace=replace(self.workspace, child_environment_applied=True),
        )


@dataclass
class InvocationEventCapture:
    usage: InvocationUsage | None = None


def emit_workflow_event(
    telemetry: ExecutionTelemetry | None,
    event_type: WorkflowEventType | NodeEventType | InvocationEventType,
    node_id: str | None = None,
    provider: str | None = None,
    role: ProviderRole | None = None,
    model: str | None = None,
    task_id: str | None = None,
    round_num: int | None = None,
    output_file: Path | None = None,
    log_file: Path | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
) -> None:
    if telemetry is None:
        return
    if event_type in {"workflow_started", "workflow_finished", "workflow_failed"}:
        event = workflow_event(
            event_type,  # type: ignore[arg-type]
            workflow_name=telemetry.workflow_name,
            run_id=telemetry.run_id,
            error=error,
        )
    elif event_type in {"node_started", "node_finished", "node_failed", "node_blocked"}:
        if node_id is None:
            raise ValueError(f"Node event '{event_type}' requires node_id.")
        event = node_event(
            event_type,  # type: ignore[arg-type]
            workflow_name=telemetry.workflow_name,
            run_id=telemetry.run_id,
            node_id=node_id,
            error=error,
        )
    else:
        if node_id is None or provider is None or role is None or task_id is None:
            raise ValueError(
                f"Invocation event '{event_type}' requires invocation context."
            )
        event = invocation_event(
            event_type,  # type: ignore[arg-type]
            workflow_name=telemetry.workflow_name,
            run_id=telemetry.run_id,
            context=ExecutionEventContext(
                workflow_name=telemetry.workflow_name,
                run_id=telemetry.run_id,
                node_id=node_id,
                provider=provider,
                role=role,
                model=model,
                task_id=task_id,
                round_num=round_num,
                output_file=str(output_file) if output_file is not None else None,
                log_file=str(log_file) if log_file is not None else None,
            ),
            duration_ms=duration_ms,
            error=error,
        )
    emit_event(telemetry.event_sink, event)


def emit_runtime_log(
    telemetry: ExecutionTelemetry | None,
    level: LogLevel,
    message: str,
    operation: str,
    context: RuntimeEventContext | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
    attributes: dict[str, RuntimeLogValue] | None = None,
) -> None:
    if telemetry is None:
        return
    event_context = (
        context.as_execution_event_context(telemetry.workflow_name, telemetry.run_id)
        if context is not None
        else None
    )
    emit_event(
        telemetry.event_sink,
        runtime_log_event(
            workflow_name=telemetry.workflow_name,
            run_id=telemetry.run_id,
            level=level,
            message=message,
            operation=operation,
            attributes=dict(attributes) if attributes is not None else None,
            duration_ms=duration_ms,
            error=error,
            context=event_context,
        ),
    )


def emit_invocation_event(
    telemetry: ExecutionTelemetry | None,
    event_type: InvocationEventType,
    metadata: InvocationMetadata,
    duration_ms: int | None = None,
    error: str | None = None,
    usage: InvocationUsage | None = None,
    failure_kind: str | None = None,
    failure_phase: str | None = None,
    failure_source: str | None = None,
    failure_advice: str | None = None,
) -> None:
    if telemetry is None:
        return
    event_context = metadata.event_context().as_execution_event_context(
        telemetry.workflow_name,
        telemetry.run_id,
    )
    usage_fields = usage.as_event_fields() if usage is not None else {}
    emit_event(
        telemetry.event_sink,
        invocation_event(
            event_type,  # type: ignore[arg-type]
            workflow_name=telemetry.workflow_name,
            run_id=telemetry.run_id,
            context=event_context,
            duration_ms=duration_ms,
            error=error,
            failure_kind=failure_kind,
            failure_phase=failure_phase,
            failure_source=failure_source,
            failure_advice=failure_advice,
            **usage_fields,
        ),
    )
    emit_workspace_context_event(telemetry, event_type, metadata)


def emit_workspace_context_event(
    telemetry: ExecutionTelemetry | None,
    event_type: InvocationEventType,
    metadata: InvocationMetadata,
) -> None:
    if telemetry is None or metadata.workspace is None:
        return
    status = INVOCATION_WORKSPACE_STATUSES.get(event_type)
    if status is None:
        return
    event_context = metadata.event_context().as_execution_event_context(
        telemetry.workflow_name,
        telemetry.run_id,
    )
    emit_event(
        telemetry.event_sink,
        workspace_event(
            "workspace_context_recorded",
            telemetry.workflow_name,
            telemetry.run_id,
            event_context,
            metadata.workspace.event_payload(status),
        ),
    )


def safe_error_message(exc: Exception) -> str:
    message = str(exc).strip()
    if message:
        return message
    return exc.__class__.__name__


def failure_event_fields(exc: Exception) -> dict[str, str]:
    if not isinstance(exc, InvocationFailureError):
        return {}
    return {
        "failure_kind": exc.kind,
        "failure_phase": exc.phase,
        "failure_source": exc.source,
        "failure_advice": exc.advice,
    }
