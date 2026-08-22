from __future__ import annotations

from typing import Any

from crewplane.core.workflow.models import WorkflowPlan
from crewplane.observability.events import (
    EventType,
    ExecutionEvent,
    ExecutionEventContext,
    WorkspaceEventPayload,
    invocation_event,
    is_invocation_event_type,
    is_node_event_type,
    is_workflow_event_type,
    node_event,
    runtime_log_event,
    workflow_event,
    workspace_event,
)
from crewplane.observability.types import (
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


def make_execution_event(event_type: EventType, **fields: Any) -> ExecutionEvent:
    workflow_name = fields.pop("workflow_name")
    run_id = fields.pop("run_id")
    match event_type:
        case _ if is_workflow_event_type(event_type):
            return workflow_event(event_type, workflow_name, run_id, **fields)
        case _ if is_node_event_type(event_type):
            return node_event(event_type, workflow_name, run_id, **fields)
        case EventType.WORKSPACE_CONTEXT_RECORDED:
            context = event_context(workflow_name, run_id, fields)
            if context is None:
                context = ExecutionEventContext(
                    workflow_name=workflow_name,
                    run_id=run_id,
                )
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
        case EventType.RUNTIME_LOG:
            context = event_context(workflow_name, run_id, fields)
            return runtime_log_event(workflow_name, run_id, context=context, **fields)
        case _ if is_invocation_event_type(event_type):
            context = event_context(workflow_name, run_id, fields)
            if context is None:
                context = ExecutionEventContext(
                    workflow_name=workflow_name,
                    run_id=run_id,
                )
            return invocation_event(
                event_type,
                workflow_name,
                run_id,
                context=context,
                **fields,
            )
        case _:
            raise ValueError(f"Unsupported execution event type: {event_type!r}.")


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
