from __future__ import annotations

from typing import Any

from orchestrator_cli.core.workflow_models import WorkflowPlan
from orchestrator_cli.observability.events import (
    ExecutionEvent,
    ExecutionEventContext,
    WorkspaceEventPayload,
    invocation_event,
    node_event,
    runtime_log_event,
    workflow_event,
    workspace_event,
)
from orchestrator_cli.observability.types import (
    TopologyNode,
    TopologyProvider,
    WorkflowTopology,
)

CONTEXT_FIELD_NAMES = frozenset(ExecutionEventContext.__dataclass_fields__) - {
    "workflow_name",
    "run_id",
}
WORKSPACE_PAYLOAD_FIELD_NAMES = frozenset(
    WorkspaceEventPayload.__dataclass_fields__,
)


def make_execution_event(**fields: Any) -> ExecutionEvent:
    event_type = fields.pop("event_type")
    workflow_name = fields.pop("workflow_name")
    run_id = fields.pop("run_id")
    if event_type in {"workflow_started", "workflow_finished", "workflow_failed"}:
        return workflow_event(event_type, workflow_name, run_id, **fields)
    if event_type in {"node_started", "node_finished", "node_failed", "node_blocked"}:
        return node_event(event_type, workflow_name, run_id, **fields)
    if event_type == "workspace_context_recorded":
        context = event_context(workflow_name, run_id, fields)
        if context is None:
            context = ExecutionEventContext(workflow_name=workflow_name, run_id=run_id)
        workspace_payload_fields = {
            key: fields.pop(key)
            for key in tuple(fields)
            if key in WORKSPACE_PAYLOAD_FIELD_NAMES
        }
        return workspace_event(
            event_type,
            workflow_name,
            run_id,
            context,
            WorkspaceEventPayload(**workspace_payload_fields),
            **fields,
        )
    if event_type == "runtime_log":
        context = event_context(workflow_name, run_id, fields)
        return runtime_log_event(workflow_name, run_id, context=context, **fields)
    return invocation_event(
        event_type,
        workflow_name,
        run_id,
        context=event_context(workflow_name, run_id, fields),
        **fields,
    )


def event_context(
    workflow_name: str,
    run_id: str,
    fields: dict[str, Any],
) -> ExecutionEventContext | None:
    context_fields = {
        key: fields.pop(key) for key in tuple(fields) if key in CONTEXT_FIELD_NAMES
    }
    if not context_fields:
        return None
    return ExecutionEventContext(
        workflow_name=workflow_name,
        run_id=run_id,
        **context_fields,
    )


def topology_from_workflow(workflow: WorkflowPlan) -> WorkflowTopology:
    return WorkflowTopology(
        workflow_name=workflow.name,
        nodes=tuple(
            TopologyNode(
                id=node.id,
                mode=node.mode,
                dependencies=tuple(node.needs),
                providers=tuple(
                    TopologyProvider(
                        provider=provider.provider,
                        model=provider.model,
                        role=provider.role,
                    )
                    for provider in node.providers
                ),
            )
            for node in workflow.nodes
        ),
    )
