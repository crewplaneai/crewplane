from __future__ import annotations

from pathlib import Path

from .models import ComposedNode, CompositionContext, NodeSpec
from .rewrites import (
    qualify_id,
    resolve_dependency_id,
    rewrite_prompt_segments,
)


def compose_local_node(
    context: CompositionContext,
    node: NodeSpec,
) -> tuple[ComposedNode, set[str]]:
    node_payload = node.payload
    composed_id = qualify_id(context.namespace_prefix, node_payload.id)
    composed_needs = tuple(
        resolve_dependency_id(
            dependency,
            namespace_prefix=context.namespace_prefix,
            bound_input_nodes=context.bound_input_nodes,
        )
        for dependency in node_payload.needs
    )

    if node_payload.mode == "input":
        return (
            ComposedNode(
                payload=node_payload.model_copy(
                    update={"id": composed_id, "needs": list(composed_needs)}
                ),
                source_path=node.source_path,
                source_span=node.source_span,
                prompt_segment_spans=node.prompt_segment_spans,
            ),
            set(),
        )

    resolved_segments, consumed_params = rewrite_prompt_segments(
        node_payload.prompt_segments,
        namespace_prefix=context.namespace_prefix,
        bound_input_nodes=context.bound_input_nodes,
        params=context.inherited_params,
        source_path=node.source_path,
        node_id=node_payload.id,
    )
    return (
        ComposedNode(
            payload=node_payload.model_copy(
                update={
                    "id": composed_id,
                    "needs": list(composed_needs),
                    "prompt_segments": resolved_segments,
                }
            ),
            source_path=node.source_path,
            source_span=node.source_span,
            prompt_segment_spans=node.prompt_segment_spans,
        ),
        consumed_params,
    )


def resolve_bound_input_nodes(
    source_path: Path,
    workflow_inputs: dict[str, str],
    bound_inputs: dict[str, str],
) -> dict[str, str]:
    if not bound_inputs:
        return {}

    resolved: dict[str, str] = {}
    for input_name, source_id in bound_inputs.items():
        raw_node_id = workflow_inputs.get(input_name)
        if raw_node_id is None:
            raise ValueError(
                f"Workflow '{source_path}' does not declare input '{input_name}'."
            )
        resolved[raw_node_id] = source_id
    return resolved
