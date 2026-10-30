"""Validate foreach sources and partition compiled inventories into frozen children."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from crewplane.core.config import Config
from crewplane.core.workflow.collections import collection_reference
from crewplane.core.workflow.composition.models import ComposedForeach, ComposedNode
from crewplane.core.workflow.graph import ancestor_map
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import WorkflowNode, WorkflowPlan
from crewplane.core.workflow.syntax import NODE_ARTIFACT_REFERENCE_PATTERN
from crewplane.core.workspace.policy import PROJECT_ROOT_WORKTREE_SELECTOR
from crewplane.core.workspace.selection import selected_worktree_name

from ..compile_state import PreflightCompileOptions
from ..compiled_records import PreflightExecutionNode
from ..models import (
    CompiledForeach,
    PreflightCompilationPreview,
    PreflightExecutionPlan,
)
from ..source import PreflightWorkflowSource
from .expansion import instantiate_foreach
from .index import projected_index
from .reconstruction import apply_expansion


def compilation_source(
    source: PreflightWorkflowSource, config: Config
) -> PreflightWorkflowSource:
    """Validate groups and return a source with parent and child inventory nodes.

    Sources without foreach nodes are reused. Otherwise, only the source and
    workflow containers are replaced; inventory projection controls node reuse.

    Raises:
        ValueError: Concurrency is unbounded, a child is missing, a reserved
            namespace or collection source is invalid, or expansion bounds fail.
            Checks follow authored group order and stop at the first failure.
    """
    groups = [node for node in source.workflow.nodes if node.mode == "foreach"]
    if not groups:
        return source
    if config.settings.max_concurrent_nodes is None:
        raise ValueError(
            "Foreach workflows require explicit finite settings.max_concurrent_nodes"
        )
    nodes = {node.id: node for node in source.workflow.nodes}
    ancestors = ancestor_map(source.workflow)
    bound = 0
    for group in groups:
        template = source.foreach_templates.get(group.id)
        if template is None:
            raise ValueError(
                f"Foreach '{group.id}' requires a composed Markdown child workflow"
            )
        _validate_namespace(source.workflow, group.id)
        _validate_collection_sources(group, nodes, ancestors[group.id])
        count = len(group.items) if isinstance(group.items, list) else group.max_items
        if count is None:
            raise ValueError(f"Foreach '{group.id}' has no max_items")
        bound += count * len(template.nodes)
    if bound > config.settings.max_expanded_nodes:
        raise ValueError(
            f"Foreach generated-node bound {bound} exceeds settings.max_expanded_nodes {config.settings.max_expanded_nodes}"
        )
    return replace(source, workflow=inventory_workflow(source))


def _validate_namespace(workflow: WorkflowPlan, group_id: str) -> None:
    prefix = f"{group_id}."
    for node in workflow.nodes:
        if node.id.startswith(prefix) or any(
            dep.startswith(prefix) for dep in node.needs
        ):
            raise ValueError(f"Foreach '{group_id}' reserves node namespace '{prefix}'")
        if any(
            match.group(1).startswith(prefix)
            for segment in node.prompt_segments
            for match in NODE_ARTIFACT_REFERENCE_PATTERN.finditer(segment.content)
        ):
            raise ValueError(
                f"Parent prompts cannot reference generated namespace '{prefix}'"
            )
    if any(name.startswith(prefix) for name in workflow.worktrees):
        raise ValueError(f"Foreach '{group_id}' reserves worktree namespace '{prefix}'")


def _validate_collection_sources(
    group: WorkflowNode, nodes: dict[str, WorkflowNode], ancestors: set[str]
) -> None:
    sources = set(group.inputs.values())
    if isinstance(group.items, str):
        producer, artifact = collection_reference(group.items)
        sources.add(producer)
        if producer in nodes:
            _validate_collection_artifact(group.id, nodes[producer], artifact)
    if not sources.issubset(ancestors):
        raise ValueError(
            f"Foreach '{group.id}' sources must be ancestors through authored needs: {', '.join(sorted(sources - ancestors))}"
        )


def _validate_collection_artifact(
    group_id: str, producer: WorkflowNode, artifact: str
) -> None:
    if artifact == "output" and producer.mode not in {"input", "foreach"}:
        raise ValueError(
            f"Foreach '{group_id}' collection output must come from an input or foreach node"
        )
    if artifact == "findings" and (
        not producer.findings
        or sum(
            provider.role == ProviderRole.EXECUTOR for provider in producer.providers
        )
        != 1
    ):
        raise ValueError(
            f"Foreach '{group_id}' requires available single-executor findings from '{producer.id}'"
        )


def inventory_workflow(source: PreflightWorkflowSource) -> WorkflowPlan:
    """Append child nodes and worktrees in template order without mutating source.

    Reuse workflows without templates, parent input/foreach nodes, and child
    input nodes. Shallow-copy other nodes, selecting parent worktrees from the
    authored workflow and child worktrees by explicit then implicit selector.
    Both fall back to the project root. Child declarations update the copied
    worktree map after parent selection.
    """
    if not source.foreach_templates:
        return source.workflow
    nodes = [
        _inventory_parent_node(source.workflow, node) for node in source.workflow.nodes
    ]
    worktrees = dict(source.workflow.worktrees)
    for template in source.foreach_templates.values():
        nodes.extend(_inventory_child_node(node) for node in template.nodes)
        worktrees.update(template.worktrees)
    return source.workflow.model_copy(update={"nodes": nodes, "worktrees": worktrees})


def _inventory_parent_node(workflow: WorkflowPlan, node: WorkflowNode) -> WorkflowNode:
    if node.mode in {"input", "foreach"}:
        return node
    return node.model_copy(
        update={
            "worktree": selected_worktree_name(workflow, node)
            or PROJECT_ROOT_WORKTREE_SELECTOR
        }
    )


def _inventory_child_node(node: ComposedNode) -> WorkflowNode:
    if node.payload.mode == "input":
        return node.payload
    return node.payload.model_copy(
        update={
            "worktree": node.payload.worktree
            or node.implicit_worktree_selector
            or PROJECT_ROOT_WORKTREE_SELECTOR
        }
    )


def finish_foreach_preview(
    source: PreflightWorkflowSource, preview: PreflightCompilationPreview
) -> PreflightCompilationPreview:
    """Freeze child templates and return a preview containing only parent records.

    Child nodes follow execution order; other records retain inventory order.
    Edges belong to their target's partition. Compiled records and unpartitioned
    resources, payloads, signatures, and secret context are shared with preview.

    Raises:
        ValueError: An authored group lacks workflow, items, or max_items.
    """
    nodes = {node.id: node for node in preview.nodes}
    authored = {node.id: node for node in source.workflow.nodes}
    templates = [
        _compile_foreach_template(authored[group_id], child, preview, nodes)
        for group_id, child in source.foreach_templates.items()
    ]
    return _parent_preview(preview, set(authored), templates)


def _compile_foreach_template(
    group: WorkflowNode,
    child: ComposedForeach,
    preview: PreflightCompilationPreview,
    nodes: dict[str, PreflightExecutionNode],
) -> CompiledForeach:
    ids = {node.payload.id for node in child.nodes}
    child_nodes = [
        nodes[node_id] for node_id in preview.execution_order if node_id in ids
    ]
    if group.workflow is None or group.items is None:
        raise ValueError(f"Incomplete foreach '{group.id}'")
    limit = group.max_items
    if limit is None:
        if not isinstance(group.items, list):
            raise ValueError(f"Incomplete foreach '{group.id}'")
        limit = len(group.items)
    template = CompiledForeach(
        group_id=group.id,
        namespace=child.namespace,
        workflow=group.workflow,
        items=group.items,
        max_items=limit,
        item_node_id=child.item_node_id,
        shared_bindings=child.shared_bindings,
        parameters=child.parameters,
        original_dependencies=list(group.needs),
        template_digest="",
        execution_order=[node.id for node in child_nodes],
        nodes=child_nodes,
        render_plans=[
            render for render in preview.render_plans if render.node_id in ids
        ],
        workspace_file_locators=[
            locator
            for locator in preview.workspace_file_locators
            if locator.node_id in ids
        ],
        token_catalog=[
            token for token in preview.token_catalog if token.node_id in ids
        ],
        dependency_graph=[
            edge for edge in preview.dependency_graph if edge.target_node in ids
        ],
    )
    template.template_digest = template.semantic_digest()
    return template


def _parent_preview(
    preview: PreflightCompilationPreview,
    authored: set[str],
    templates: list[CompiledForeach],
) -> PreflightCompilationPreview:
    return preview.model_copy(
        update={
            "foreach_templates": templates,
            "nodes": [node for node in preview.nodes if node.id in authored],
            "execution_order": [
                node_id for node_id in preview.execution_order if node_id in authored
            ],
            "render_plans": [
                render for render in preview.render_plans if render.node_id in authored
            ],
            "workspace_file_locators": [
                locator
                for locator in preview.workspace_file_locators
                if locator.node_id in authored
            ],
            "token_catalog": [
                token for token in preview.token_catalog if token.node_id in authored
            ],
            "dependency_graph": [
                edge
                for edge in preview.dependency_graph
                if edge.target_node in authored
            ],
        }
    )


def validate_literal_expansions(
    preview: PreflightCompilationPreview, options: PreflightCompileOptions
) -> None:
    """Validate literal groups cumulatively on an in-memory plan without I/O.

    Referenced collections remain deferred. The preview is unchanged; each
    literal expansion updates only the local plan before checking its projected
    result index, so later groups share the generated-node budget.

    Raises:
        ValueError: Plan, collection, expansion, branch, or result-index
            validation fails.
    """
    plan = PreflightExecutionPlan.from_preview(
        preview,
        "preflight",
        "preflight",
        str(options.project_root),
        str(options.project_root),
        str(options.state_dir),
        datetime.now(UTC),
    )
    for template in plan.foreach_templates:
        if isinstance(template.items, list):
            batch = instantiate_foreach(plan, template, template.items, {})
            plan = apply_expansion(plan, batch)
            projected_index(plan, template, template.items)
