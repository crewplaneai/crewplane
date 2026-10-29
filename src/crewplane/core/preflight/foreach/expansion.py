"""Derive foreach identities and ordered record copies without publishing artifacts.

Validated expansion checks collections and budgets; reconstruction can instead
derive the same records from existing evidence with ``concrete_batch``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

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
    Fragment,
    PreflightExecutionNode,
    RenderPlan,
    RenderStream,
    TokenCatalogEntry,
    WorkspaceFileLocator,
)
from ..dependency_edges import dependency_signature
from ..models import CompiledForeach, PreflightExecutionPlan
from ..serialization import canonical_json_bytes
from ..signatures import signature_for_payload
from ..workspace.models import WorkspaceSelectionRecord
from .expansion_identity import expansion_digest
from .models import ExpansionBatch


def expansion_directory(group_id: str) -> str:
    """Return the stage-relative expansion directory using artifact name rules."""
    return f"preflight/expansions/{build_stage_directory_name(group_id)}"


def descriptor(path: str, data: bytes) -> ExpansionArtifact:
    """Describe bytes without I/O; reject unsafe relative artifact paths."""
    return ExpansionArtifact(
        relative_path=path,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def generated_id(template: CompiledForeach, node_id: str, ordinal: int) -> str:
    """Qualify a frozen node or worktree name for the supplied item ordinal.

    The caller supplies a valid ordinal; names outside the template namespace
    retain their full spelling as the local suffix.
    """
    local_id = node_id.removeprefix(f"{template.namespace}.")
    return f"{template.group_id}.i{ordinal}.{local_id}"


def expansion_limit(plan: PreflightExecutionPlan) -> int:
    """Return the compiled node budget or raise ValueError unless strictly positive."""
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
    """Validate items, derive their evidence, and construct an unpublished batch.

    Requires a validated plan containing the template and its group. Collection
    errors precede duplicate-group and cumulative-budget ValueErrors. Inputs are
    unchanged; caller artifact descriptors are retained in a new evidence mapping.
    """
    _validate_expansion(plan, template, items)
    state = _expansion_state(plan, template, items, caller_artifacts)
    group = next(node for node in plan.nodes if node.id == template.group_id)
    return concrete_batch(template, state, group.dependencies)


def _validate_expansion(
    plan: PreflightExecutionPlan, template: CompiledForeach, items: list[JsonObject]
) -> None:
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


def _expansion_state(
    plan: PreflightExecutionPlan,
    template: CompiledForeach,
    items: list[JsonObject],
    caller_artifacts: dict[str, ExpansionArtifact],
) -> ExpansionState:
    root = expansion_directory(template.group_id)
    collection = descriptor(
        f"{root}/items.json",
        collection_bytes(items, template.max_items, template.group_id),
    )
    item_descriptors = [
        descriptor(f"{root}/items/{index}.json", canonical_json_bytes(item))
        for index, item in enumerate(items)
    ]
    return ExpansionState(
        group_id=template.group_id,
        workflow_signature=plan.workflow_signature,
        template_digest=template.template_digest,
        digest=expansion_digest(
            template,
            plan.workflow_signature,
            collection,
            item_descriptors,
            caller_artifacts,
        ),
        collection=collection,
        items=item_descriptors,
        caller_artifacts=caller_artifacts,
    )


def concrete_batch(
    template: CompiledForeach, state: ExpansionState, dependencies: list[str]
) -> ExpansionBatch:
    """Derive records from trusted template/state evidence without validating it.

    Unlike ``instantiate_foreach``, this does not check collection contents,
    expansion identity, or budgets. Retain state, copy group dependencies, and
    append items by ordinal with each inventory in template order. Remapped
    records are shallow copies; untouched nested policy/provider data is shared.
    No input records are mutated and no artifacts are written.
    """
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
    """Build a signed scheduling or artifact edge without modifying graph records."""
    return DependencyEdge(
        source_node=source,
        target_node=target,
        artifact_name=artifact,
        artifact_key=artifact,
        target_locator=f"{source}.{artifact}" if artifact else source,
        dependency_signature=dependency_signature(source, target, artifact),
    )


def terminal_node_ids(template: CompiledForeach) -> set[str]:
    """Return terminal membership without imposing an inventory or execution order.

    Requires a validated frozen graph. The item slot is a result only when it is
    the sole node. The returned set is new and the template remains unchanged.
    """
    local_ids = {node.id for node in template.nodes}
    terminal_ids = local_ids - {
        edge.source_node
        for edge in template.dependency_graph
        if edge.source_node in local_ids
    }
    if len(local_ids) > 1:
        terminal_ids.discard(template.item_node_id)
    return terminal_ids


@dataclass(frozen=True)
class _InstanceMapping:
    template: CompiledForeach
    ordinal: int
    node_ids: dict[str, str]
    item_id: str
    terminal_ids: set[str]
    locator_ids: dict[str, str]


def _instance_mapping(template: CompiledForeach, ordinal: int) -> _InstanceMapping:
    node_ids = {
        node.id: generated_id(template, node.id, ordinal) for node in template.nodes
    }
    item_id = node_ids[template.item_node_id]
    terminal_ids = terminal_node_ids(template)
    locator_ids = {
        locator.locator_id: f"workspace-file-{signature_for_payload([locator.locator_id, item_id])}"
        for locator in template.workspace_file_locators
    }
    return _InstanceMapping(
        template, ordinal, node_ids, item_id, terminal_ids, locator_ids
    )


def _append_instance(
    batch: ExpansionBatch, template: CompiledForeach, ordinal: int
) -> None:
    instance = _instance_mapping(template, ordinal)
    _append_nodes(batch, instance)
    _append_artifact_edges(batch, instance)
    batch.render_plans.extend(
        _remap_render(render, instance) for render in template.render_plans
    )
    batch.workspace_file_locators.extend(
        _remap_workspace_locator(locator, instance)
        for locator in template.workspace_file_locators
    )
    batch.token_catalog.extend(
        _remap_token(token, instance) for token in template.token_catalog
    )


def _append_nodes(batch: ExpansionBatch, instance: _InstanceMapping) -> None:
    for original in instance.template.nodes:
        node = _remap_node(original, batch.state, instance)
        batch.nodes.append(node)
        if original.id in instance.terminal_ids:
            batch.group_dependencies.append(node.id)
        for dependency in node.dependencies:
            batch.dependency_graph.append(scheduling_edge(dependency, node.id))


def _remap_node(
    original: PreflightExecutionNode, state: ExpansionState, instance: _InstanceMapping
) -> PreflightExecutionNode:
    node_id = instance.node_ids[original.id]
    dependencies = _instance_dependencies(original, instance)
    policy = _remap_workspace_policy(original.workspace_policy, instance)
    return original.model_copy(
        update={
            "id": node_id,
            "dependencies": dependencies,
            "render_plan_id": instance.node_ids.get(original.render_plan_id or ""),
            "workspace_policy": policy,
            "expansion": state.provenance(instance.ordinal),
            "artifact_contract": artifact_contract_for_node(node_id, original.findings),
            "input_item_slot": False,
            "input_content_ref": state.items[instance.ordinal].relative_path
            if original.id == instance.template.item_node_id
            else original.input_content_ref,
            "input_workspace_file_locator_id": instance.locator_ids.get(
                original.input_workspace_file_locator_id or ""
            ),
        }
    )


def _instance_dependencies(
    original: PreflightExecutionNode, instance: _InstanceMapping
) -> list[str]:
    dependencies = [instance.node_ids.get(dep, dep) for dep in original.dependencies]
    if original.id == instance.template.item_node_id:
        dependencies = list(instance.template.original_dependencies)
    elif instance.node_ids.keys().isdisjoint(original.dependencies):
        dependencies.append(instance.item_id)
    return list(dict.fromkeys(dependencies))


def _remap_workspace_policy(
    policy: WorkspaceSelectionRecord | None, instance: _InstanceMapping
) -> WorkspaceSelectionRecord | None:
    if policy is None:
        return None
    return policy.model_copy(
        update={
            "logical_worktree_name": generated_id(
                instance.template, policy.logical_worktree_name or "", instance.ordinal
            ),
            "source_node_id": instance.node_ids.get(
                policy.source_node_id or "", policy.source_node_id
            ),
        }
    )


def _append_artifact_edges(batch: ExpansionBatch, instance: _InstanceMapping) -> None:
    for edge in instance.template.dependency_graph:
        if edge.artifact_name is not None:
            batch.dependency_graph.append(
                scheduling_edge(
                    instance.node_ids.get(edge.source_node, edge.source_node),
                    instance.node_ids[edge.target_node],
                    edge.artifact_name,
                )
            )


def _remap_workspace_locator(
    locator: WorkspaceFileLocator, instance: _InstanceMapping
) -> WorkspaceFileLocator:
    return locator.model_copy(
        update={
            "node_id": instance.node_ids[locator.node_id],
            "locator_id": instance.locator_ids[locator.locator_id],
            "occurrence_id": _occurrence(
                locator.occurrence_id, locator.node_id, instance.node_ids
            ),
        }
    )


def _remap_render(render: RenderPlan, instance: _InstanceMapping) -> RenderPlan:
    streams = [_remap_stream(stream, instance) for stream in render.streams]
    return render.model_copy(
        update={
            "node_id": instance.node_ids[render.node_id or ""],
            "render_plan_id": instance.node_ids[render.render_plan_id],
            "streams": streams,
            "template_signature": signature_for_payload(
                {
                    "node_id": instance.node_ids[render.node_id or ""],
                    "template_hash": render.template_hash,
                }
            ),
        }
    )


def _remap_stream(stream: RenderStream, instance: _InstanceMapping) -> RenderStream:
    return stream.model_copy(
        update={
            "fragments": [
                _remap_fragment(fragment, instance) for fragment in stream.fragments
            ]
        }
    )


def _remap_fragment(fragment: Fragment, instance: _InstanceMapping) -> Fragment:
    locator = fragment.locator
    if locator is not None:
        locator = dict(locator)
        if fragment.kind == "runtime_locator_lookup":
            locator["node_id"] = instance.node_ids.get(
                locator["node_id"], locator["node_id"]
            )
        if fragment.kind == "workspace_file_locator":
            locator["locator_id"] = instance.locator_ids[locator["locator_id"]]
    return fragment.model_copy(update={"locator": locator})


def _remap_token(
    token: TokenCatalogEntry, instance: _InstanceMapping
) -> TokenCatalogEntry:
    updates: dict[str, object] = {
        "node_id": instance.node_ids[token.node_id],
        "occurrence_id": _occurrence(
            token.occurrence_id, token.node_id, instance.node_ids
        ),
    }
    if token.token_kind == "node":
        updates.update(_node_token_updates(token, instance))
    else:
        updates.update(_workspace_token_updates(token, instance))
    updates["signature"] = signature_for_payload(
        [token.signature, instance.node_ids[token.node_id]]
    )
    return token.model_copy(update=updates)


def _node_token_updates(
    token: TokenCatalogEntry, instance: _InstanceMapping
) -> dict[str, object]:
    resolved, metadata = dict(token.resolved), dict(token.metadata)
    source = metadata["node_id"]
    source = instance.node_ids.get(source, source)
    artifact = metadata["artifact_name"]
    signature = dependency_signature(source, instance.node_ids[token.node_id], artifact)
    metadata.update(node_id=source, dependency_signature=signature)
    resolved.update(
        locator={"node_id": source, "artifact_name": artifact},
        dependency_signature=signature,
    )
    return {
        "canonical_locator": f"{source}.{artifact}",
        "dependency_signature": signature,
        "raw_token": rewrite_artifact_references(
            token.raw_token, "", instance.node_ids
        ),
        "resolved": resolved,
        "metadata": metadata,
    }


def _workspace_token_updates(
    token: TokenCatalogEntry, instance: _InstanceMapping
) -> dict[str, object]:
    resolved, metadata = dict(token.resolved), dict(token.metadata)
    updates: dict[str, object] = {"resolved": resolved, "metadata": metadata}
    if token.canonical_locator in instance.locator_ids:
        locator_id = instance.locator_ids[token.canonical_locator]
        updates["canonical_locator"] = locator_id
        for payload in (resolved, metadata):
            if "locator_id" in payload:
                payload["locator_id"] = locator_id
            if "node_id" in payload:
                payload["node_id"] = instance.node_ids[token.node_id]
    return updates


def _occurrence(occurrence: str, node_id: str, mapping: dict[str, str]) -> str:
    return mapping[node_id] + occurrence.removeprefix(node_id)
