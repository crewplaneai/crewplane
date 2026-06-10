from __future__ import annotations

import hashlib
import json
import stat
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from orchestrator_cli.core.execution_state import (
    RUN_STATUS_SUCCEEDED,
    ArtifactDescriptor,
    NodeState,
)
from orchestrator_cli.core.preflight.models import (
    PreflightExecutionNode,
    PreflightExecutionPlan,
)

from .naming import build_node_state_filename
from .run_history import RunHistoryRecord

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
    descriptors = {descriptor.kind: descriptor for descriptor in node_state.artifacts}
    required = required_resume_artifact_paths(plan, node)
    if not required:
        return False
    return all(
        _descriptor_matches_file(source.results_dir, descriptors.get(kind), expected)
        for kind, expected in required.items()
    )


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
    return hashlib.sha256(artifact_path.read_bytes()).hexdigest() == descriptor.sha256


def contained_regular_file(root: Path, relative_path: str) -> Path | None:
    raw_parts = relative_path.split("/")
    if (
        not relative_path
        or any(part in {"", ".", ".."} for part in raw_parts)
        or Path(relative_path).is_absolute()
    ):
        return None
    if _has_symlink_component(root):
        return None
    path = Path(*raw_parts)
    candidate = root
    for part in path.parts:
        candidate = candidate / part
        if _path_is_symlink(candidate):
            return None
    try:
        resolved = candidate.resolve(strict=True)
        root_resolved = root.resolve(strict=True)
    except PermissionError:
        raise
    except OSError:
        return None
    if not resolved.is_relative_to(root_resolved):
        return None
    try:
        stat = resolved.stat()
    except PermissionError:
        raise
    except OSError:
        return None
    if not resolved.is_file() or stat.st_nlink != 1:
        return None
    return resolved


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor) if path.is_absolute() else Path()
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for part in parts:
        current = current / part
        if _path_is_symlink(current):
            return True
    return False


def _path_is_symlink(path: Path) -> bool:
    try:
        return stat.S_ISLNK(path.lstat().st_mode)
    except PermissionError:
        raise
    except OSError:
        return False


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
