from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from orchestrator_cli.architecture.contracts import JsonValue
from orchestrator_cli.core.execution_state import (
    RUN_STATUS_SUCCEEDED,
    ArtifactDescriptor,
    NodeState,
)
from orchestrator_cli.core.file_hashing import sha256_file
from orchestrator_cli.core.preflight.models import (
    PreflightExecutionNode,
    PreflightExecutionPlan,
)

from ..naming import build_node_state_filename
from ..run_history import RunHistoryRecord
from ..safe_files import contained_regular_file
from ..workspace.node_state import build_node_workspace_descriptor
from ..workspace.state.validation import workspace_node_state_is_valid
from .generated_files import generated_file_path_belongs_to_node

_FINDINGS_KEYS = {
    "findings",
    "findings_path",
    "findings_size",
    "findings_sha256",
}


@dataclass(frozen=True)
class ValidatedResumeFrontier:
    source: RunHistoryRecord
    node_states: dict[str, NodeState]

    @property
    def resumed_node_ids(self) -> tuple[str, ...]:
        return tuple(self.node_states)


@dataclass(frozen=True)
class _WorkspaceDescriptorStore:
    run_id: str
    run_key_name: str
    task_name: str
    stages_dir: Path
    results_dir: Path
    logs_dir: Path
    project_root: Path
    log_cli_output: bool
    stage_name: str
    stage_dir: Path

    def get_stage_dir(self, stage_name: str) -> Path | None:
        if stage_name != self.stage_name or not self.stage_dir.is_dir():
            return None
        return self.stage_dir


def validate_resume_frontier(
    source: RunHistoryRecord,
    plan: PreflightExecutionPlan,
) -> ValidatedResumeFrontier:
    nodes_by_id = {node.id: node for node in plan.nodes}
    dependencies = _dependencies_by_node(plan)
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

    return ValidatedResumeFrontier(
        source=source,
        node_states=_dependency_closed_states(valid_states, dependencies, nodes_by_id),
    )


def _read_node_state(source: RunHistoryRecord, node_id: str) -> NodeState | None:
    node_state_path = contained_regular_file(
        source.run_dir,
        f"manifests/nodes/{build_node_state_filename(node_id)}",
    )
    if node_state_path is None:
        return None
    try:
        payload = json.loads(node_state_path.read_text(encoding="utf-8"))
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
    store = _WorkspaceDescriptorStore(
        run_id=source.manifest.run_id,
        run_key_name=source.manifest.run_key_name,
        task_name=source.manifest.workflow_name,
        stages_dir=source.run_dir,
        results_dir=source.results_dir,
        logs_dir=source.run_dir / "logs",
        project_root=source.run_dir,
        log_cli_output=False,
        stage_name=node.id,
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
        "size_bytes": _json_int(artifact.get("resume_size_bytes")),
    }


def _json_string(value: JsonValue | None) -> str | None:
    return value if isinstance(value, str) else None


def _json_int(value: JsonValue | None) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


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
) -> dict[str, str]:
    required = {"output": node.artifact_contract.output_path}
    findings_required = any(
        edge.source_node == node.id and edge.artifact_name in _FINDINGS_KEYS
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
    dependents = {node_id: set() for node_id in dependencies}
    for node_id, node_dependencies in dependencies.items():
        for dependency in node_dependencies:
            dependents.setdefault(dependency, set()).add(node_id)
    return dependents


def _dependencies_by_node(plan: PreflightExecutionPlan) -> dict[str, set[str]]:
    dependencies = {node.id: set() for node in plan.nodes}
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
    return list(nodes_by_id).index(node_id)
