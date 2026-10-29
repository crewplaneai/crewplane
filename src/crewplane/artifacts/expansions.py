"""Publish committed expansions and reconstruct graphs from verified bundles."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from crewplane.architecture.contracts import JsonObject, NodeArtifactRequest
from crewplane.architecture.ports.artifacts import ArtifactStorePort
from crewplane.architecture.safe_file_reads import read_contained_bytes
from crewplane.core.execution_state import NodeState
from crewplane.core.expansion_state import ExpansionArtifact
from crewplane.core.preflight.compiled_records import PreflightExecutionNode
from crewplane.core.preflight.foreach.expansion import (
    descriptor,
    expansion_directory,
    instantiate_foreach,
)
from crewplane.core.preflight.foreach.index import projected_index
from crewplane.core.preflight.foreach.models import ExpansionBatch
from crewplane.core.preflight.foreach.reconstruction import apply_expansion
from crewplane.core.preflight.models import CompiledForeach, PreflightExecutionPlan
from crewplane.core.preflight.plan_signatures import (
    semantic_node_payloads,
    semantic_source_location_payload,
)
from crewplane.core.preflight.serialization import canonical_json_bytes
from crewplane.core.workflow.collections import (
    COLLECTION_BYTE_LIMIT,
    collection_reference,
    decode_collection,
)

from .naming import node_state_relative_path
from .verification import read_verified_node_artifact


@dataclass
class ReconstructedExpansions:
    plan: PreflightExecutionPlan
    batches: dict[str, ExpansionBatch] = field(default_factory=dict)
    invalid_groups: set[str] = field(default_factory=set)


def caller_locators(template: CompiledForeach) -> set[tuple[str, str]]:
    locators = {(caller, "output") for caller in template.shared_bindings.values()}
    local = {node.id for node in template.nodes}
    for edge in template.dependency_graph:
        if edge.source_node not in local and edge.artifact_name is not None:
            locators.add(
                (
                    edge.source_node,
                    "findings"
                    if edge.artifact_name.startswith("findings")
                    else "output",
                )
            )
    if isinstance(template.items, str):
        locators.add(collection_reference(template.items))
    return locators


def read_expansion_artifact(
    root: Path, evidence: ExpansionArtifact, max_bytes: int | None = None
) -> bytes:
    if max_bytes is not None and evidence.size_bytes > max_bytes:
        raise ValueError("Expansion artifact exceeds byte limit")
    payload = read_contained_bytes(root, evidence.relative_path, evidence.size_bytes)
    if (
        len(payload) != evidence.size_bytes
        or hashlib.sha256(payload).hexdigest() != evidence.sha256
    ):
        raise ValueError(
            f"Expansion artifact does not match descriptor: {evidence.relative_path}"
        )
    return payload


def verify_caller_artifacts(
    plan: PreflightExecutionPlan,
    batch: ExpansionBatch,
    stages_dir: Path,
    results_dir: Path,
) -> None:
    template = next(
        template
        for template in plan.foreach_templates
        if template.group_id == batch.state.group_id
    )
    nodes = {node.id: node for node in plan.nodes}
    expected = {f"{node_id}.{kind}" for node_id, kind in caller_locators(template)}
    if set(batch.state.caller_artifacts) != expected:
        raise ValueError("Expansion caller evidence differs from compiled bindings")
    for node_id, kind in caller_locators(template):
        node = nodes[node_id]
        verify_node_expansion(stages_dir, node)
        is_collection = isinstance(template.items, str) and (
            node_id,
            kind,
        ) == collection_reference(template.items)
        artifact = read_verified_node_artifact(
            stages_dir,
            results_dir,
            NodeArtifactRequest(node_id, node.artifact_contract),
            kind,
            COLLECTION_BYTE_LIMIT if is_collection else None,
        )
        evidence = descriptor(
            artifact.path.relative_to(results_dir).as_posix(), artifact.payload
        )
        if evidence != batch.state.caller_artifacts[f"{node_id}.{kind}"]:
            raise ValueError("Expansion caller artifact identity changed")
        if is_collection:
            items = decode_collection(
                artifact.payload,
                template.max_items,
                template.group_id,
                str(template.items),
            )
            if (
                descriptor(
                    batch.state.collection.relative_path, canonical_json_bytes(items)
                )
                != batch.state.collection
            ):
                raise ValueError(
                    "Expansion collection differs from its caller artifact"
                )


def verify_node_expansion(stages_dir: Path, node: PreflightExecutionNode) -> None:
    state = NodeState.model_validate_json(
        read_contained_bytes(stages_dir, node_state_relative_path(node.id).as_posix())
    )
    if (
        state.node_id != node.id
        or state.status != "succeeded"
        or state.expansion != node.expansion
    ):
        raise ValueError(
            f"Node '{node.id}' success does not match expansion provenance"
        )


def commit_expansion(
    output: ArtifactStorePort,
    plan: PreflightExecutionPlan,
    batch: ExpansionBatch,
    items: list[JsonObject],
    publish: Callable[[Path], None],
) -> tuple[Path, ...]:
    template = next(
        template
        for template in plan.foreach_templates
        if template.group_id == batch.state.group_id
    )
    derived = instantiate_foreach(plan, template, items, batch.state.caller_artifacts)
    if batch != derived:
        raise ValueError("Expansion additions differ from frozen-template derivation")
    effective = apply_expansion(plan, batch)
    projected_index(effective, template, items)
    verify_caller_artifacts(plan, batch, output.stages_dir, output.results_dir)
    publications = [
        output.write_preflight_static_file(
            batch.state.collection.relative_path.removeprefix("preflight/"),
            canonical_json_bytes(items),
        )
    ]
    publish(publications[-1])
    for evidence, item in zip(batch.state.items, items, strict=True):
        publications.append(
            output.write_preflight_static_file(
                evidence.relative_path.removeprefix("preflight/"),
                canonical_json_bytes(item),
            )
        )
        publish(publications[-1])
    publications.append(
        output.write_preflight_json(
            f"{expansion_directory(template.group_id).removeprefix('preflight/')}/expansion.json",
            batch,
        )
    )
    publish(publications[-1])
    return tuple(publications)


def reconstruct_expansions(
    plan: PreflightExecutionPlan, stages_dir: Path
) -> ReconstructedExpansions:
    result = ReconstructedExpansions(plan)
    templates = {template.group_id: template for template in plan.foreach_templates}
    for group_id in plan.execution_order:
        template = templates.get(group_id)
        if template is None:
            continue
        if group_id in result.plan.expansions:
            continue
        try:
            batch, items = load_expansion(result.plan, template, stages_dir)
            effective = apply_expansion(result.plan, batch)
            projected_index(effective, template, items)
        except (OSError, ValueError, RecursionError):
            result.invalid_groups.add(group_id)
            continue
        result.batches[group_id] = batch
        result.plan = effective
    return result


def load_expansion(
    plan: PreflightExecutionPlan, template: CompiledForeach, stages_dir: Path
) -> tuple[ExpansionBatch, list[JsonObject]]:
    path = f"{expansion_directory(template.group_id)}/expansion.json"
    recorded = ExpansionBatch.model_validate_json(
        read_contained_bytes(stages_dir, path)
    )
    items = decode_collection(
        read_expansion_artifact(
            stages_dir, recorded.state.collection, COLLECTION_BYTE_LIMIT
        ),
        template.max_items,
        template.group_id,
    )
    derived = instantiate_foreach(
        plan, template, items, recorded.state.caller_artifacts
    )
    if recorded.state != derived.state:
        raise ValueError("Expansion identity or item descriptors changed")
    expected_callers = caller_locators(template)
    nodes = {node.id: node for node in plan.nodes}
    if set(recorded.state.caller_artifacts) != {
        f"{node}.{kind}" for node, kind in expected_callers
    }:
        raise ValueError("Expansion caller bindings changed")
    for node_id, kind in expected_callers:
        contract = nodes[node_id].artifact_contract
        expected_path = (
            contract.output_path if kind == "output" else contract.findings_path
        )
        if (
            recorded.state.caller_artifacts[f"{node_id}.{kind}"].relative_path
            != expected_path
        ):
            raise ValueError("Expansion caller locator changed")
    if isinstance(template.items, list) and items != template.items:
        raise ValueError("Literal expansion items differ from compiled collection")
    for evidence, item in zip(recorded.state.items, items, strict=True):
        if read_expansion_artifact(
            stages_dir, evidence, COLLECTION_BYTE_LIMIT
        ) != canonical_json_bytes(item):
            raise ValueError("Expansion item file differs from collection")
    recorded_payload, derived_payload = recorded.model_dump(), derived.model_dump()
    recorded_payload["nodes"] = semantic_node_payloads(recorded.nodes)
    derived_payload["nodes"] = semantic_node_payloads(derived.nodes)
    if semantic_source_location_payload(
        recorded_payload
    ) != semantic_source_location_payload(derived_payload):
        raise ValueError(
            "Recorded expansion additions differ from frozen-template derivation"
        )
    return derived, items
