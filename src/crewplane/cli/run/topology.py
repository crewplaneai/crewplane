from __future__ import annotations

from collections.abc import Iterable

from crewplane.core.preflight import (
    PreflightCompilationPreview,
    PreflightExecutionNode,
    PreflightExecutionPlan,
)
from crewplane.observability.types import (
    TopologyNode,
    TopologyProvider,
    WorkflowTopology,
)


def workflow_topology_from_nodes(
    workflow_name: str,
    nodes: Iterable[PreflightExecutionNode],
) -> WorkflowTopology:
    return WorkflowTopology(
        workflow_name=workflow_name,
        nodes=tuple(
            TopologyNode(
                id=node.id,
                mode=node.mode,
                dependencies=tuple(node.dependencies),
                providers=tuple(
                    TopologyProvider(
                        provider=provider.provider,
                        model=provider.model,
                        role=provider.role,
                    )
                    for provider in node.provider_records
                ),
            )
            for node in nodes
        ),
    )


def workflow_topology_from_plan(plan: PreflightExecutionPlan) -> WorkflowTopology:
    return workflow_topology_from_nodes(plan.workflow_name, plan.nodes)


def workflow_topology_from_preview(
    preview: PreflightCompilationPreview,
) -> WorkflowTopology:
    if preview.workflow_name is None:
        raise ValueError("Successful preflight preview requires workflow_name.")
    return workflow_topology_from_nodes(preview.workflow_name, preview.nodes)
