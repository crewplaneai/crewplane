from __future__ import annotations

from typing import Any

from orchestrator_cli.core.workflow_models import WorkflowPlan
from orchestrator_cli.observability.events import (
    ExecutionEvent,
    invocation_event,
    node_event,
    runtime_log_event,
    workflow_event,
)
from orchestrator_cli.observability.types import (
    TopologyNode,
    TopologyProvider,
    WorkflowTopology,
)


def make_execution_event(**fields: Any) -> ExecutionEvent:
    event_type = fields.pop("event_type")
    workflow_name = fields.pop("workflow_name")
    run_id = fields.pop("run_id")
    if event_type in {"workflow_started", "workflow_finished", "workflow_failed"}:
        return workflow_event(event_type, workflow_name, run_id, **fields)
    if event_type in {"node_started", "node_finished", "node_failed", "node_blocked"}:
        return node_event(event_type, workflow_name, run_id, **fields)
    if event_type == "runtime_log":
        return runtime_log_event(workflow_name, run_id, **fields)
    return invocation_event(event_type, workflow_name, run_id, **fields)


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
