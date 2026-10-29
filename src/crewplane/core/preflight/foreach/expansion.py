from __future__ import annotations

import hashlib

from crewplane.architecture.contracts import (
    JsonObject,
    artifact_contract_for_node,
    build_stage_directory_name,
)
from crewplane.core.expansion_state import ExpansionArtifact, ExpansionState
from crewplane.core.workflow.collections import collection_bytes, validate_collection
from crewplane.core.workflow.composition.rewrites import rewrite_artifact_references

from ..compiled_records import (
    DependencyEdge,
    RenderPlan,
    TokenCatalogEntry,
)
from ..dependency_edges import dependency_signature
from ..models import CompiledForeach, PreflightExecutionPlan
from ..serialization import canonical_json_bytes
from ..signatures import signature_for_payload
from .models import ExpansionBatch


def expansion_directory(group_id: str) -> str:
    return f"preflight/expansions/{build_stage_directory_name(group_id)}"


def descriptor(path: str, data: bytes) -> ExpansionArtifact:
    return ExpansionArtifact(
        relative_path=path,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def generated_id(template: CompiledForeach, node_id: str, ordinal: int) -> str:
    local_id = node_id.removeprefix(f"{template.namespace}.")
    return f"{template.group_id}.i{ordinal}.{local_id}"


def expansion_limit(plan: PreflightExecutionPlan) -> int:
    execution = plan.runtime_config_snapshot.get("execution")
    limit = execution.get("max_expanded_nodes") if isinstance(execution, dict) else None
    if type(limit) is not int or limit <= 0:
        raise ValueError("Foreach requires a compiled positive max_expanded_nodes")
    return limit


def instantiate_foreach(
    plan: PreflightExecutionPlan,
    template: CompiledForeach,
    items: list[JsonObject],
    caller_artifacts: dict[str, ExpansionArtifact],
) -> ExpansionBatch:
    validate_collection(items, template.max_items, template.group_id)
    if template.group_id in plan.expansions:
        raise ValueError(f"Foreach '{template.group_id}' is already expanded")
    restored = sum(
        len(state.items)
        * len(next(t.nodes for t in plan.foreach_templates if t.group_id == group))
        for group, state in plan.expansions.items()
    )
    if restored + len(items) * len(template.nodes) > expansion_limit(plan):
        raise ValueError(
            f"Foreach '{template.group_id}' exceeds cumulative max_expanded_nodes"
        )
    root = expansion_directory(template.group_id)
    collection = descriptor(
        f"{root}/items.json",
        collection_bytes(items, template.max_items, template.group_id),
    )
    item_descriptors = [
        descriptor(f"{root}/items/{index}.json", canonical_json_bytes(item))
        for index, item in enumerate(items)
    ]
    identity = {
        "workflow_signature": plan.workflow_signature,
        "template_digest": template.template_digest,
        "group_id": template.group_id,
        "collection": collection,
        "items": item_descriptors,
        "caller_artifacts": caller_artifacts,
        "shared_bindings": template.shared_bindings,
    }
    state = ExpansionState(
        group_id=template.group_id,
        workflow_signature=plan.workflow_signature,
        template_digest=template.template_digest,
        digest=signature_for_payload(identity),
        collection=collection,
        items=item_descriptors,
        caller_artifacts=caller_artifacts,
    )
    group = next(node for node in plan.nodes if node.id == template.group_id)
    return concrete_batch(template, state, group.dependencies)


def concrete_batch(
    template: CompiledForeach, state: ExpansionState, dependencies: list[str]
) -> ExpansionBatch:
    batch = ExpansionBatch(
        state=state,
        nodes=[],
        render_plans=[],
        workspace_file_locators=[],
        token_catalog=[],
        dependency_graph=[],
        group_dependencies=list(dependencies),
    )
    for ordinal in range(len(state.items)):
        _append_instance(batch, template, ordinal)
    return batch


def scheduling_edge(
    source: str, target: str, artifact: str | None = None
) -> DependencyEdge:
    return DependencyEdge(
        source_node=source,
        target_node=target,
        artifact_name=artifact,
        artifact_key=artifact,
        target_locator=f"{source}.{artifact}" if artifact else source,
        dependency_signature=dependency_signature(source, target, artifact),
    )


def _append_instance(
    batch: ExpansionBatch, template: CompiledForeach, ordinal: int
) -> None:
    mapping = {
        node.id: generated_id(template, node.id, ordinal) for node in template.nodes
    }
    item_id = mapping[template.item_node_id]
    local_ids = set(mapping)
    terminal_ids = local_ids - {
        edge.source_node
        for edge in template.dependency_graph
        if edge.source_node in local_ids
    }
    if len(local_ids) > 1:
        terminal_ids.discard(template.item_node_id)
    locator_ids = {
        locator.locator_id: f"workspace-file-{signature_for_payload([locator.locator_id, item_id])}"
        for locator in template.workspace_file_locators
    }
    for original in template.nodes:
        node_id = mapping[original.id]
        dependencies = [mapping.get(dep, dep) for dep in original.dependencies]
        if original.id == template.item_node_id:
            dependencies = list(template.original_dependencies)
        elif not local_ids.intersection(original.dependencies):
            dependencies.append(item_id)
        policy = original.workspace_policy
        if policy is not None:
            policy = policy.model_copy(
                update={
                    "logical_worktree_name": generated_id(
                        template, policy.logical_worktree_name or "", ordinal
                    ),
                    "source_node_id": mapping.get(
                        policy.source_node_id or "", policy.source_node_id
                    ),
                }
            )
        node = original.model_copy(
            update={
                "id": node_id,
                "dependencies": list(dict.fromkeys(dependencies)),
                "render_plan_id": mapping.get(original.render_plan_id or ""),
                "workspace_policy": policy,
                "expansion": batch.state.provenance(ordinal),
                "artifact_contract": artifact_contract_for_node(
                    node_id, original.findings
                ),
                "input_item_slot": False,
                "input_content_ref": batch.state.items[ordinal].relative_path
                if original.id == template.item_node_id
                else original.input_content_ref,
                "input_workspace_file_locator_id": locator_ids.get(
                    original.input_workspace_file_locator_id or ""
                ),
            }
        )
        batch.nodes.append(node)
        if original.id in terminal_ids:
            batch.group_dependencies.append(node_id)
        for dependency in node.dependencies:
            batch.dependency_graph.append(scheduling_edge(dependency, node_id))
    for edge in template.dependency_graph:
        if edge.artifact_name is not None:
            batch.dependency_graph.append(
                scheduling_edge(
                    mapping.get(edge.source_node, edge.source_node),
                    mapping[edge.target_node],
                    edge.artifact_name,
                )
            )
    batch.render_plans.extend(
        _remap_render(render, mapping, locator_ids) for render in template.render_plans
    )
    batch.workspace_file_locators.extend(
        locator.model_copy(
            update={
                "node_id": mapping[locator.node_id],
                "locator_id": locator_ids[locator.locator_id],
                "occurrence_id": _occurrence(
                    locator.occurrence_id, locator.node_id, mapping
                ),
            }
        )
        for locator in template.workspace_file_locators
    )
    batch.token_catalog.extend(
        _remap_token(token, mapping, locator_ids) for token in template.token_catalog
    )


def _occurrence(occurrence: str, node_id: str, mapping: dict[str, str]) -> str:
    return mapping[node_id] + occurrence.removeprefix(node_id)


def _remap_render(
    render: RenderPlan, mapping: dict[str, str], locators: dict[str, str]
) -> RenderPlan:
    streams = []
    for stream in render.streams:
        fragments = []
        for fragment in stream.fragments:
            locator = fragment.locator
            if locator is not None:
                locator = dict(locator)
                if fragment.kind == "runtime_locator_lookup":
                    locator["node_id"] = mapping.get(
                        locator["node_id"], locator["node_id"]
                    )
                if fragment.kind == "workspace_file_locator":
                    locator["locator_id"] = locators[locator["locator_id"]]
            fragments.append(fragment.model_copy(update={"locator": locator}))
        streams.append(stream.model_copy(update={"fragments": fragments}))
    return render.model_copy(
        update={
            "node_id": mapping[render.node_id or ""],
            "render_plan_id": mapping[render.render_plan_id],
            "streams": streams,
            "template_signature": signature_for_payload(
                {
                    "node_id": mapping[render.node_id or ""],
                    "template_hash": render.template_hash,
                }
            ),
        }
    )


def _remap_token(
    token: TokenCatalogEntry, mapping: dict[str, str], locators: dict[str, str]
) -> TokenCatalogEntry:
    updates: dict[str, object] = {
        "node_id": mapping[token.node_id],
        "occurrence_id": _occurrence(token.occurrence_id, token.node_id, mapping),
    }
    resolved, metadata = dict(token.resolved), dict(token.metadata)
    if token.token_kind == "node":
        source = metadata["node_id"]
        source = mapping.get(source, source)
        artifact = metadata["artifact_name"]
        signature = dependency_signature(source, mapping[token.node_id], artifact)
        metadata.update(node_id=source, dependency_signature=signature)
        resolved.update(
            locator={"node_id": source, "artifact_name": artifact},
            dependency_signature=signature,
        )
        updates.update(
            canonical_locator=f"{source}.{artifact}",
            dependency_signature=signature,
            raw_token=rewrite_artifact_references(token.raw_token, "", mapping),
        )
    elif token.canonical_locator in locators:
        updates["canonical_locator"] = locators[token.canonical_locator]
        for payload in (resolved, metadata):
            if "locator_id" in payload:
                payload["locator_id"] = locators[token.canonical_locator]
            if "node_id" in payload:
                payload["node_id"] = mapping[token.node_id]
    updates.update(
        resolved=resolved,
        metadata=metadata,
        signature=signature_for_payload([token.signature, mapping[token.node_id]]),
    )
    return token.model_copy(update=updates)
