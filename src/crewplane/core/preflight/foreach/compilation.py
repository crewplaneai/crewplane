from __future__ import annotations

from dataclasses import replace
from datetime import UTC

from crewplane.core.config import Config
from crewplane.core.workflow.collections import collection_reference
from crewplane.core.workflow.graph import ancestor_map
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import WorkflowPlan
from crewplane.core.workflow.syntax import NODE_ARTIFACT_REFERENCE_PATTERN
from crewplane.core.workspace.policy import PROJECT_ROOT_WORKTREE_SELECTOR
from crewplane.core.workspace.selection import selected_worktree_name

from ..compile_state import PreflightCompileOptions
from ..models import CompiledForeach, PreflightCompilationPreview
from ..plan_signatures import semantic_node_payloads, semantic_source_location_payload
from ..signatures import signature_for_payload
from ..source import PreflightWorkflowSource


def compilation_source(
    source: PreflightWorkflowSource, config: Config
) -> PreflightWorkflowSource:
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
        prefix = f"{group.id}."
        for node in source.workflow.nodes:
            if node.id.startswith(prefix) or any(
                dep.startswith(prefix) for dep in node.needs
            ):
                raise ValueError(
                    f"Foreach '{group.id}' reserves node namespace '{prefix}'"
                )
            if any(
                match.group(1).startswith(prefix)
                for segment in node.prompt_segments
                for match in NODE_ARTIFACT_REFERENCE_PATTERN.finditer(segment.content)
            ):
                raise ValueError(
                    f"Parent prompts cannot reference generated namespace '{prefix}'"
                )
        if any(name.startswith(prefix) for name in source.workflow.worktrees):
            raise ValueError(
                f"Foreach '{group.id}' reserves worktree namespace '{prefix}'"
            )
        sources = set(group.inputs.values())
        if isinstance(group.items, str):
            producer, artifact = collection_reference(group.items)
            sources.add(producer)
            if producer in nodes:
                producer_node = nodes[producer]
                if artifact == "output" and producer_node.mode not in {
                    "input",
                    "foreach",
                }:
                    raise ValueError(
                        f"Foreach '{group.id}' collection output must come from an input or foreach node"
                    )
                if artifact == "findings" and (
                    not producer_node.findings
                    or sum(
                        provider.role == ProviderRole.EXECUTOR
                        for provider in producer_node.providers
                    )
                    != 1
                ):
                    raise ValueError(
                        f"Foreach '{group.id}' requires available single-executor findings from '{producer}'"
                    )
        if not sources.issubset(ancestors[group.id]):
            raise ValueError(
                f"Foreach '{group.id}' sources must be ancestors through authored needs: {', '.join(sorted(sources - ancestors[group.id]))}"
            )
        count = len(group.items) if isinstance(group.items, list) else group.max_items
        if count is None:
            raise ValueError(f"Foreach '{group.id}' has no max_items")
        bound += count * len(template.nodes)
    if bound > config.settings.max_expanded_nodes:
        raise ValueError(
            f"Foreach generated-node bound {bound} exceeds settings.max_expanded_nodes {config.settings.max_expanded_nodes}"
        )
    return replace(source, workflow=inventory_workflow(source))


def inventory_workflow(source: PreflightWorkflowSource) -> WorkflowPlan:
    if not source.foreach_templates:
        return source.workflow
    nodes = [
        node.model_copy(
            update={
                "worktree": selected_worktree_name(source.workflow, node)
                or PROJECT_ROOT_WORKTREE_SELECTOR
            }
        )
        if node.mode not in {"input", "foreach"}
        else node
        for node in source.workflow.nodes
    ]
    worktrees = dict(source.workflow.worktrees)
    for template in source.foreach_templates.values():
        nodes.extend(
            node.payload.model_copy(
                update={
                    "worktree": node.payload.worktree
                    or node.implicit_worktree_selector
                    or PROJECT_ROOT_WORKTREE_SELECTOR
                }
            )
            if node.payload.mode != "input"
            else node.payload
            for node in template.nodes
        )
        worktrees.update(template.worktrees)
    return source.workflow.model_copy(update={"nodes": nodes, "worktrees": worktrees})


def finish_foreach_preview(
    source: PreflightWorkflowSource, preview: PreflightCompilationPreview
) -> PreflightCompilationPreview:
    templates: list[CompiledForeach] = []
    nodes = {node.id: node for node in preview.nodes}
    authored = {node.id: node for node in source.workflow.nodes}
    for group_id, child in source.foreach_templates.items():
        group = authored[group_id]
        ids = {node.payload.id for node in child.nodes}
        child_nodes = [
            nodes[node_id] for node_id in preview.execution_order if node_id in ids
        ]
        renders = [render for render in preview.render_plans if render.node_id in ids]
        locators = [
            locator
            for locator in preview.workspace_file_locators
            if locator.node_id in ids
        ]
        tokens = [token for token in preview.token_catalog if token.node_id in ids]
        edges = [edge for edge in preview.dependency_graph if edge.target_node in ids]
        digest = signature_for_payload(
            {
                "nodes": semantic_node_payloads(child_nodes),
                "render_plans": semantic_source_location_payload(renders),
                "locators": semantic_source_location_payload(locators),
                "tokens": semantic_source_location_payload(tokens),
                "edges": edges,
                "item_node_id": child.item_node_id,
                "bindings": child.shared_bindings,
                "parameters": child.parameters,
            }
        )
        if group.workflow is None or group.items is None or group.max_items is None:
            raise ValueError(f"Incomplete foreach '{group_id}'")
        templates.append(
            CompiledForeach(
                group_id=group_id,
                namespace=child.namespace,
                workflow=group.workflow,
                items=group.items,
                max_items=group.max_items,
                item_node_id=child.item_node_id,
                shared_bindings=child.shared_bindings,
                parameters=child.parameters,
                original_dependencies=list(group.needs),
                template_digest=digest,
                execution_order=[node.id for node in child_nodes],
                nodes=child_nodes,
                render_plans=renders,
                workspace_file_locators=locators,
                token_catalog=tokens,
                dependency_graph=edges,
            )
        )
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
    from datetime import datetime

    from ..models import PreflightExecutionPlan
    from .expansion import instantiate_foreach
    from .index import projected_index
    from .reconstruction import apply_expansion

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
