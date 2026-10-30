"""Materialize composed nodes and worktree selections into workflow results."""

from __future__ import annotations

from crewplane.core.workspace.policy import (
    PROJECT_ROOT_WORKTREE_SELECTOR,
    worktree_declarations_payload,
)

from ..models import WorkflowPayload, workflow_node_payload_dict
from .models import (
    ComposedForeach,
    ComposedNode,
    CompositionResult,
    ParamBinding,
    ParsedWorkflow,
)
from .rewrites import qualify_id


def materialize_workflow_payload(
    workflow: ParsedWorkflow, composition: CompositionResult
) -> WorkflowPayload:
    """Build a root payload without mutating the parsed or composed workflow.

    Args:
        workflow: Parsed root supplying metadata, inputs, repetition settings,
            and the local worktree count.
        composition: Completed traversal with node IDs, references, and worktree
            names already qualified. Node and worktree order is preserved.

    Returns:
        A new payload with copied inputs, serialized nodes and worktrees, and
        implicit worktree selectors materialized where needed. Serialization
        retains foreach ``items`` collections by reference, so this is not a
        deep copy.

    Raises:
        ValueError: A composed foreach node has an incomplete declaration.
    """
    nodes = _materialize_implicit_worktree_selectors(
        composition.nodes, len(composition.worktrees), len(workflow.worktrees)
    )
    payload: WorkflowPayload = {
        "schema_version": workflow.schema_version,
        "name": workflow.name,
        "description": workflow.description,
        "inputs": dict(workflow.inputs),
        "nodes": [workflow_node_payload_dict(node.payload) for node in nodes],
    }
    if workflow.repeat_force_run_count is not None:
        payload["repeat_force_run_count"] = workflow.repeat_force_run_count
    if composition.worktrees:
        payload["worktrees"] = worktree_declarations_payload(composition.worktrees)
    return payload


def materialize_foreach_template(
    group_id: str,
    item_id: str,
    workflow: ParsedWorkflow,
    params: dict[str, ParamBinding],
    shared_bindings: dict[str, str],
    composition: CompositionResult,
) -> ComposedForeach:
    """Build a foreach template from an already validated child traversal.

    Args:
        group_id: Fully qualified parent group ID.
        item_id: Unqualified child input node ID, prefixed with
            ``<group_id>.template`` in the returned template.
        workflow: Parsed child supplying the local worktree count.
        params: Effective inherited and overridden parameter bindings.
        shared_bindings: Child input bindings resolved in the caller's namespace.
        composition: Child nodes and worktree names already qualified under
            ``<group_id>.template``, with bound input fallbacks omitted.

    Returns:
        A template in composition order with a new dictionary of consumed
        parameter values. Worktrees and shared bindings are retained by
        reference. The node list is retained when there are no worktrees;
        otherwise a new list reuses nodes except those needing explicit worktree
        selectors. Replaced nodes have shallow-copied payloads and retain source
        metadata references. No supplied collection or node is mutated.
    """
    namespace = f"{group_id}.template"
    return ComposedForeach(
        group_id=group_id,
        namespace=namespace,
        item_node_id=qualify_id(namespace, item_id),
        nodes=_materialize_implicit_worktree_selectors(
            composition.nodes, len(composition.worktrees), len(workflow.worktrees)
        ),
        worktrees=composition.worktrees,
        shared_bindings=shared_bindings,
        parameters={
            key: binding.value
            for key, binding in params.items()
            if binding.binding_id in composition.consumed_param_bindings
        },
    )


def _materialize_implicit_worktree_selectors(
    nodes: list[ComposedNode],
    composed_worktree_count: int,
    root_worktree_count: int,
) -> list[ComposedNode]:
    if composed_worktree_count == 0:
        return nodes
    return [
        _materialize_worktree_selector(node)
        if _needs_materialized_selector(
            node,
            composed_worktree_count,
            root_worktree_count,
        )
        else node
        for node in nodes
    ]


def _needs_materialized_selector(
    node: ComposedNode,
    composed_worktree_count: int,
    root_worktree_count: int,
) -> bool:
    if node.payload.mode in {"input", "foreach"}:
        return False
    if node.payload.worktree is not None:
        return False
    if node.local_worktree_count == 0:
        if node.implicit_worktree_selector is not None:
            return composed_worktree_count > 1
        return root_worktree_count != 1 or composed_worktree_count > 1
    return node.implicit_worktree_selector is not None and composed_worktree_count > 1


def _materialize_worktree_selector(node: ComposedNode) -> ComposedNode:
    return ComposedNode(
        payload=node.payload.model_copy(
            update={
                "worktree": node.implicit_worktree_selector
                or PROJECT_ROOT_WORKTREE_SELECTOR
            }
        ),
        source_path=node.source_path,
        source_span=node.source_span,
        prompt_segment_spans=node.prompt_segment_spans,
        local_worktree_count=node.local_worktree_count,
        implicit_worktree_selector=node.implicit_worktree_selector,
    )
