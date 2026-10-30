"""Project validated collection caps into findings-producer prompt segments."""

from crewplane.core.prompt_segments import PromptSegment, PromptSegmentRole
from crewplane.core.workflow.collections import collection_reference
from crewplane.core.workflow.models import WorkflowNode, WorkflowPlan

COLLECTION_CONSTRAINT_INSTRUCTION = (
    "\n\nExecutor findings: JSON array with at most {max_items} items.\n"
)


def with_collection_constraints(workflow: WorkflowPlan) -> WorkflowPlan:
    """Append shared guidance without mutating authored nodes or segments."""
    limits = _producer_limits(workflow)
    if not limits:
        return workflow
    nodes: list[WorkflowNode] = []
    for node in workflow.nodes:
        if node.id not in limits:
            nodes.append(node)
            continue
        constraint = PromptSegment(
            role=PromptSegmentRole.SHARED,
            content=COLLECTION_CONSTRAINT_INSTRUCTION.format(max_items=limits[node.id]),
        )
        nodes.append(
            node.model_copy(
                update={"prompt_segments": [*node.prompt_segments, constraint]}
            )
        )
    return workflow.model_copy(update={"nodes": nodes})


def _producer_limits(workflow: WorkflowPlan) -> dict[str, int]:
    limits: dict[str, int] = {}
    for group in workflow.nodes:
        if group.mode != "foreach" or not isinstance(group.items, str):
            continue
        producer, artifact = collection_reference(group.items)
        if artifact == "findings":
            assert group.max_items is not None
            limits[producer] = min(
                limits.get(producer, group.max_items), group.max_items
            )
    return limits
