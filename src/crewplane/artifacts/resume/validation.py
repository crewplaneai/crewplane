"""Select dependency-closed resume states and eligible review checkpoints."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from crewplane.architecture.contracts import JsonValue
from crewplane.architecture.safe_file_reads import read_contained_bytes
from crewplane.architecture.safe_files import contained_regular_file
from crewplane.core.execution_state import (
    RUN_STATUS_SUCCEEDED,
    ArtifactDescriptor,
    ArtifactKind,
    NodeState,
)
from crewplane.core.file_hashing import sha256_file
from crewplane.core.preflight.compiled_records import PreflightExecutionNode
from crewplane.core.preflight.foreach.models import ExpansionBatch
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.core.review_checkpoint import (
    OpenReviewCheckpoint,
)
from crewplane.core.review_checkpoint_state import CheckpointProjectObservation
from crewplane.core.value_checks import optional_strict_int
from crewplane.core.workflow.keywords import FINDINGS_ARTIFACT_KEYS

from ..expansions import reconstruct_expansions, verify_caller_artifacts
from ..generated_files.paths import generated_file_path_belongs_to_node
from ..naming import node_state_relative_path
from ..run_history import RunHistoryRecord
from ..workspace.node_state import (
    WorkspaceDescriptorLookup,
    build_node_workspace_descriptor,
)
from ..workspace.state.validation import workspace_node_state_is_valid
from .checkpoint_selection import select_resume_checkpoints
from .expansions import eligible_expansion_plan


@dataclass(frozen=True)
class ValidatedResumeFrontier:
    """Reusable state and checkpoint fences from one source run.

    ``node_states`` contains dependency-closed successful states. Its insertion
    order and that of the open ``checkpoints`` mapping follow ``plan.nodes``;
    the ID properties expose those orders. Checkpoints require all upstream
    states to be reusable and do not make their own node's results reusable.

    ``closed_checkpoint_ids`` contains identity- and topology-valid closed
    markers, including fenced or completed nodes. History selection uses these
    IDs to fence older open checkpoints without blocking successful-state reuse.
    """

    source: RunHistoryRecord
    node_states: dict[str, NodeState]
    checkpoints: dict[str, OpenReviewCheckpoint] = field(default_factory=dict)
    closed_checkpoint_ids: frozenset[str] = frozenset()
    effective_plan: PreflightExecutionPlan | None = None
    expansion_batches: dict[str, ExpansionBatch] = field(default_factory=dict)

    @property
    def resumed_node_ids(self) -> tuple[str, ...]:
        """Return reusable node IDs in ``node_states`` insertion order."""
        return tuple(self.node_states)

    @property
    def checkpoint_node_ids(self) -> tuple[str, ...]:
        """Return open checkpoint node IDs in ``checkpoints`` insertion order."""
        return tuple(self.checkpoints)


def validate_resume_frontier(
    source: RunHistoryRecord,
    plan: PreflightExecutionPlan,
    project_observation: CheckpointProjectObservation | None = None,
    fenced_checkpoint_ids: frozenset[str] = frozenset(),
) -> ValidatedResumeFrontier:
    """Select reusable artifacts synchronously without writing or hydrating them.

    Scan states in ``plan.nodes`` order, invalidate rejected states' descendants,
    and enforce dependency closure before scanning checkpoints in the same order.
    Returned state and open-checkpoint mappings retain that order.

    Checkpoint source identity and node validity precede closed-marker collection
    and the open-marker gates: completed node, ``fenced_checkpoint_ids``, and
    missing reusable prerequisites. Eligible open markers are checked for
    provenance, file dependencies, then project observation. ``project_observation``
    supplies the current unmanaged-project fingerprint; managed nodes ignore it.

    Missing or malformed states are unavailable. State/artifact inspection keeps
    its validators' error handling: uncaught filesystem errors propagate, while
    workspace validators may reject state. Checkpoint ValueError, OSError, and
    RuntimeError failures reject only that marker; other exceptions propagate.
    """
    base_plan = plan
    reconstruction = reconstruct_expansions(base_plan, source.run_dir)
    plan = reconstruction.plan
    nodes_by_id = {node.id: node for node in plan.nodes}
    dependencies = _dependencies_by_node(plan)
    closed_states = _select_resume_states(source, plan, dependencies, nodes_by_id)
    eligible_groups: set[str] = set()
    templates = {template.group_id: template for template in plan.foreach_templates}
    for group_id, batch in reconstruction.batches.items():
        template = templates[group_id]
        eligible = set(template.original_dependencies).issubset(closed_states)
        if eligible:
            try:
                verify_caller_artifacts(plan, batch, source.run_dir, source.results_dir)
            except (OSError, ValueError):
                eligible = False
        if eligible:
            eligible_groups.add(group_id)
        else:
            for node in plan.nodes:
                if node.expansion is not None and node.expansion.group_id == group_id:
                    closed_states.pop(node.id, None)
            closed_states = _dependency_closed_states(
                closed_states, dependencies, nodes_by_id
            )
    plan = eligible_expansion_plan(base_plan, reconstruction.batches, eligible_groups)
    checkpoints, closed_ids = select_resume_checkpoints(
        source,
        reconstruction.plan,
        _dependencies_by_node(reconstruction.plan),
        closed_states,
        project_observation,
        fenced_checkpoint_ids,
    )
    return ValidatedResumeFrontier(
        source,
        closed_states,
        checkpoints,
        closed_ids,
        plan,
        {
            group: batch
            for group, batch in reconstruction.batches.items()
            if group in eligible_groups
        },
    )


def _select_resume_states(
    source: RunHistoryRecord,
    plan: PreflightExecutionPlan,
    dependencies: dict[str, set[str]],
    nodes_by_id: dict[str, PreflightExecutionNode],
) -> dict[str, NodeState]:
    dependents = _dependents_by_node(dependencies)
    invalid_nodes: set[str] = set()
    valid_states: dict[str, NodeState] = {}

    for node in plan.nodes:
        node_state = _read_node_state(source, node.id)
        if node_state is None:
            continue
        if _node_state_is_valid(source, plan, node, node_state):
            valid_states[node.id] = node_state
        else:
            invalid_nodes.add(node.id)

    invalid_nodes.update(_descendants(invalid_nodes, dependents))
    for node_id in invalid_nodes:
        valid_states.pop(node_id, None)

    return _dependency_closed_states(valid_states, dependencies, nodes_by_id)


def _read_node_state(source: RunHistoryRecord, node_id: str) -> NodeState | None:
    node_state_path = contained_regular_file(
        source.run_dir,
        node_state_relative_path(node_id).as_posix(),
    )
    if node_state_path is None:
        return None
    try:
        payload = json.loads(
            read_contained_bytes(node_state_path.parent, node_state_path.name)
        )
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    try:
        return NodeState.model_validate(payload)
    except ValidationError:
        return None


def _node_state_is_valid(
    source: RunHistoryRecord,
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    node_state: NodeState,
) -> bool:
    if node_state.expansion != node.expansion or (
        node.mode == "foreach" and node.expansion is None
    ):
        return False
    if not _node_state_matches_context(source, plan, node.id, node_state):
        return False
    descriptors = _artifact_descriptors(node_state)
    required = required_resume_artifact_paths(plan, node)
    if not required:
        return False
    artifacts_match = all(
        _descriptor_matches_file(source.results_dir, descriptors.get(kind), expected)
        for kind, expected in required.items()
    )
    if not artifacts_match:
        return False
    if not _generated_file_descriptors_match(
        source.results_dir,
        node_state.generated_files,
        node.id,
    ):
        return False
    if not workspace_node_state_is_valid(source, plan, node):
        return False
    return _workspace_manifest_descriptor_matches(source, plan, node, node_state)


def _artifact_descriptors(node_state: NodeState) -> dict[str, ArtifactDescriptor]:
    return {descriptor.kind: descriptor for descriptor in node_state.artifacts}


def _generated_file_descriptors_match(
    results_dir: Path,
    descriptors: list[ArtifactDescriptor],
    node_id: str,
) -> bool:
    return all(
        descriptor.kind == "generated_file"
        and generated_file_path_belongs_to_node(descriptor.relative_path, node_id)
        and _descriptor_matches_file(
            results_dir,
            descriptor,
            descriptor.relative_path,
        )
        for descriptor in descriptors
    )


def _workspace_manifest_descriptor_matches(
    source: RunHistoryRecord,
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    node_state: NodeState,
) -> bool:
    policy = node.workspace_policy
    if policy is None or not policy.enabled:
        return node_state.workspace is None
    if node_state.workspace is None:
        return False
    stage_path = node.artifact_contract.stage_path
    if stage_path is None:
        return False
    store = WorkspaceDescriptorLookup(
        stages_dir=source.run_dir,
        node_id=node.id,
        stage_dir=source.run_dir / stage_path,
    )
    try:
        expected = build_node_workspace_descriptor(node, plan, store)
    except (OSError, RuntimeError, ValueError):
        return False
    return _workspace_resume_descriptor(node_state.workspace) == (
        _workspace_resume_descriptor(expected)
    )


def _workspace_resume_descriptor(workspace: JsonValue) -> JsonValue:
    if isinstance(workspace, Mapping):
        return _workspace_resume_mapping(workspace)
    if isinstance(workspace, list):
        return [_workspace_resume_descriptor(item) for item in workspace]
    return workspace


def _workspace_resume_mapping(
    workspace: Mapping[str, JsonValue],
) -> dict[str, JsonValue]:
    normalized: dict[str, JsonValue] = {}
    for key, value in workspace.items():
        if key == "branch_export":
            continue
        if key == "workspace_state_artifact" and isinstance(value, Mapping):
            normalized[key] = _workspace_state_artifact_resume_descriptor(value)
            continue
        normalized[key] = _workspace_resume_descriptor(value)
    return normalized


def _workspace_state_artifact_resume_descriptor(
    artifact: Mapping[str, JsonValue],
) -> dict[str, JsonValue]:
    return {
        "relative_path": _json_string(artifact.get("relative_path")),
        "sha256": _json_string(artifact.get("resume_sha256")),
        "size_bytes": optional_strict_int(artifact.get("resume_size_bytes")),
    }


def _json_string(value: JsonValue | None) -> str | None:
    return value if isinstance(value, str) else None


def _node_state_matches_context(
    source: RunHistoryRecord,
    plan: PreflightExecutionPlan,
    node_id: str,
    node_state: NodeState,
) -> bool:
    return (
        node_state.status == RUN_STATUS_SUCCEEDED
        and node_state.workflow_identity == source.manifest.workflow_identity
        and node_state.workflow_name == plan.workflow_name
        and node_state.workflow_signature == plan.workflow_signature
        and node_state.run_id == source.manifest.run_id
        and node_state.run_key_name == source.manifest.run_key_name
        and node_state.node_id == node_id
        and node_state.plan_schema_version == plan.plan_schema_version
    )


def required_resume_artifact_paths(
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
) -> dict[ArtifactKind, str]:
    """Return required result paths in copy order: output, then findings.

    Findings are required by ``node.findings`` or an outgoing dependency using a
    findings artifact key. Return an empty mapping when findings are required
    but the contract has no findings path; this sentinel makes state ineligible
    for reuse. This function performs no filesystem checks or mutations.
    """
    required: dict[ArtifactKind, str] = {"output": node.artifact_contract.output_path}
    findings_required = node.findings or any(
        edge.source_node == node.id and edge.artifact_name in FINDINGS_ARTIFACT_KEYS
        for edge in plan.dependency_graph
    )
    if findings_required:
        findings_path = node.artifact_contract.findings_path
        if findings_path is None:
            return {}
        required["findings"] = findings_path
    return required


def _descriptor_matches_file(
    results_dir: Path,
    descriptor: ArtifactDescriptor | None,
    expected_relative_path: str,
) -> bool:
    if descriptor is None or descriptor.relative_path != expected_relative_path:
        return False
    artifact_path = contained_regular_file(results_dir, descriptor.relative_path)
    if artifact_path is None:
        return False
    stat = artifact_path.stat()
    if stat.st_size != descriptor.size_bytes:
        return False
    return sha256_file(artifact_path) == descriptor.sha256


def _dependents_by_node(
    dependencies: dict[str, set[str]],
) -> dict[str, set[str]]:
    dependents: dict[str, set[str]] = {node_id: set() for node_id in dependencies}
    for node_id, node_dependencies in dependencies.items():
        for dependency in node_dependencies:
            dependents.setdefault(dependency, set()).add(node_id)
    return dependents


def _dependencies_by_node(plan: PreflightExecutionPlan) -> dict[str, set[str]]:
    dependencies: dict[str, set[str]] = {node.id: set() for node in plan.nodes}
    for edge in plan.dependency_graph:
        if edge.target_node in dependencies:
            dependencies[edge.target_node].add(edge.source_node)
    return dependencies


def _descendants(
    node_ids: set[str],
    dependents: dict[str, set[str]],
) -> set[str]:
    descendants: set[str] = set()
    stack = list(node_ids)
    while stack:
        node_id = stack.pop()
        for dependent in dependents.get(node_id, set()):
            if dependent in descendants:
                continue
            descendants.add(dependent)
            stack.append(dependent)
    return descendants


def _dependency_closed_states(
    states: dict[str, NodeState],
    dependencies: dict[str, set[str]],
    nodes_by_id: dict[str, PreflightExecutionNode],
) -> dict[str, NodeState]:
    closed = dict(states)
    changed = True
    while changed:
        changed = False
        for node_id in list(closed):
            if node_id not in nodes_by_id:
                closed.pop(node_id)
                changed = True
                continue
            if dependencies[node_id].issubset(closed):
                continue
            closed.pop(node_id)
            changed = True
    return {
        node_id: closed[node_id]
        for node_id in sorted(closed, key=lambda item: plan_order(nodes_by_id, item))
    }


def plan_order(nodes_by_id: dict[str, PreflightExecutionNode], node_id: str) -> int:
    """Return a node's zero-based position in the mapping's insertion order.

    Raise ValueError for an absent ID. The caller builds this mapping from
    ``plan.nodes``; ``plan.execution_order`` is not consulted.
    """
    return list(nodes_by_id).index(node_id)
