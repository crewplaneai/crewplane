from __future__ import annotations

from crewplane.core.preflight.models import (
    PreflightExecutionNode,
    PreflightExecutionPlan,
)


def workspace_plan_node(
    plan: PreflightExecutionPlan,
    node_id: str,
) -> PreflightExecutionNode:
    """Return a compiled workspace source node or raise the canonical error."""

    for node in plan.nodes:
        if node.id == node_id:
            return node
    raise RuntimeError(f"Workspace source references unknown node '{node_id}'.")
