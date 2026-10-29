from __future__ import annotations

from .models import ParsedWorkflow


def resolve_foreach_inputs(
    workflow: ParsedWorkflow, item_input: str, shared: dict[str, str], group: str
) -> tuple[str, dict[str, str]]:
    bound_nodes: dict[str, str] = {}
    for name in (item_input, *shared):
        node_id = workflow.inputs.get(name)
        if node_id is None:
            raise ValueError(f"Foreach '{group}' has unknown child input '{name}'")
        if node_id in bound_nodes:
            raise ValueError(
                f"Foreach '{group}' inputs '{name}' and '{bound_nodes[node_id]}' bind the same child node '{node_id}'"
            )
        bound_nodes[node_id] = name
    return workflow.inputs[item_input], {
        workflow.inputs[name]: caller for name, caller in shared.items()
    }
