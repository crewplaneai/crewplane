from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from crewplane.architecture.contracts import JsonObject, JsonValue
from crewplane.architecture.ports import ArtifactStorePort
from crewplane.core.execution_state import (
    RUN_STATE_SCHEMA_VERSION,
    ArtifactDescriptor,
    NodeState,
    ResumeOrigin,
)
from crewplane.core.preflight.models import (
    PreflightExecutionNode,
    PreflightExecutionPlan,
)
from crewplane.core.value_checks import is_strict_int

from ..atomic import atomic_write_bytes
from ..workspace.node_state import build_node_workspace_descriptor
from .generated_files import copy_generated_file_descriptors
from .validation import (
    ValidatedResumeFrontier,
    contained_regular_file,
    required_resume_artifact_paths,
)


def hydrate_resume_frontier(
    frontier: ValidatedResumeFrontier,
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
) -> tuple[str, ...]:
    nodes_by_id = {node.id: node for node in plan.nodes}
    for node_id in frontier.resumed_node_ids:
        node = nodes_by_id[node_id]
        source_state = frontier.node_states[node_id]
        hydrated_at = datetime.now().isoformat()
        hydrated_descriptors = _copy_descriptors(
            source_state,
            frontier,
            output,
            plan,
            node,
        )
        hydrated_generated_files = copy_generated_file_descriptors(
            frontier.source.results_dir,
            output,
            source_state.generated_files,
            node.id,
        )
        _copy_workspace_artifacts(frontier, output, node, hydrated_at)
        output.write_resume_source(
            node_id,
            _resume_source_payload(
                frontier,
                node_id,
                hydrated_descriptors,
                hydrated_at,
            ),
        )
        output.write_node_success_state(
            _hydrated_node_state(
                node,
                plan,
                output,
                hydrated_descriptors,
                hydrated_generated_files,
                frontier,
                hydrated_at,
            )
        )
    return frontier.resumed_node_ids


def _copy_descriptors(
    source_state: NodeState,
    frontier: ValidatedResumeFrontier,
    output: ArtifactStorePort,
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
) -> list[ArtifactDescriptor]:
    hydrated: list[ArtifactDescriptor] = []
    source_descriptors = {
        descriptor.kind: descriptor for descriptor in source_state.artifacts
    }
    for kind, relative_path in required_resume_artifact_paths(plan, node).items():
        descriptor = source_descriptors.get(kind)
        if descriptor is None or descriptor.relative_path != relative_path:
            raise ValueError(
                f"Validated resume descriptor missing for node '{node.id}'."
            )
        source_path = contained_regular_file(
            frontier.source.results_dir,
            descriptor.relative_path,
        )
        if source_path is None:
            raise ValueError(f"Resume artifact for node '{node.id}' is not reusable.")
        payload = source_path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != descriptor.sha256:
            raise ValueError(f"Resume artifact hash changed for node '{node.id}'.")
        if len(payload) != descriptor.size_bytes:
            raise ValueError(f"Resume artifact size changed for node '{node.id}'.")
        target_path = output.results_dir / descriptor.relative_path
        atomic_write_bytes(target_path, payload)
        hydrated.append(
            ArtifactDescriptor(
                kind=descriptor.kind,
                relative_path=descriptor.relative_path,
                size_bytes=target_path.stat().st_size,
                sha256=hashlib.sha256(target_path.read_bytes()).hexdigest(),
            )
        )
    return hydrated


def _copy_workspace_artifacts(
    frontier: ValidatedResumeFrontier,
    output: ArtifactStorePort,
    node: PreflightExecutionNode,
    hydrated_at: str,
) -> None:
    source_state = frontier.node_states[node.id]
    if node.workspace_policy is None:
        return
    stage_path = node.artifact_contract.stage_path
    if stage_path is None:
        return
    source_stage_dir = frontier.source.run_dir / stage_path
    if not source_stage_dir.is_dir():
        return
    target_stage_dir = output.create_stage_dir(node.id)
    expected_artifacts = _workspace_artifact_descriptors(source_state.workspace)
    for run_relative_path in sorted(expected_artifacts):
        relative = _stage_relative_workspace_artifact_path(
            stage_path,
            run_relative_path,
        )
        if relative is None:
            raise ValueError(
                f"Workspace resume artifact for node '{node.id}' is not reusable."
            )
        source_path = contained_regular_file(
            frontier.source.run_dir,
            run_relative_path,
        )
        if source_path is None:
            raise ValueError(
                f"Workspace resume artifact for node '{node.id}' is not reusable."
            )
        target_path = target_stage_dir / relative
        payload = source_path.read_bytes()
        _validate_workspace_artifact_payload(
            node.id,
            run_relative_path,
            payload,
            expected_artifacts,
        )
        if relative.name.startswith("workspace-state") and relative.suffix == ".json":
            payload = _hydrated_workspace_state_payload(
                payload,
                frontier,
                output,
                node.id,
                hydrated_at,
            )
        atomic_write_bytes(target_path, payload)


def _stage_relative_workspace_artifact_path(
    stage_path: str,
    run_relative_path: str,
) -> Path | None:
    prefix = f"{stage_path.rstrip('/')}/"
    if not run_relative_path.startswith(prefix):
        return None
    relative = Path(run_relative_path.removeprefix(prefix))
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        return None
    return relative


def _workspace_artifact_descriptors(
    workspace: JsonObject | None,
) -> dict[str, Mapping[str, object]]:
    descriptors: dict[str, Mapping[str, object]] = {}
    if not isinstance(workspace, Mapping):
        return descriptors
    states = workspace.get("states")
    if not isinstance(states, list):
        return descriptors
    for state in states:
        if not isinstance(state, Mapping):
            continue
        _collect_workspace_artifact_descriptor(
            descriptors,
            state.get("workspace_state_artifact"),
        )
        setup = state.get("setup")
        if isinstance(setup, Mapping):
            _collect_workspace_artifact_descriptor(
                descriptors,
                setup.get("metadata_artifact"),
            )
            _collect_workspace_artifact_descriptor(
                descriptors,
                setup.get("log_artifact"),
            )
        bundle = state.get("bundle")
        if isinstance(bundle, Mapping):
            _collect_workspace_artifact_descriptor(
                descriptors,
                bundle.get("artifact"),
            )
    review_loop = workspace.get("review_loop")
    if isinstance(review_loop, Mapping):
        _collect_workspace_artifact_descriptor(
            descriptors,
            review_loop.get("status_artifact"),
        )
        selected_outputs = review_loop.get("selected_outputs")
        if isinstance(selected_outputs, list):
            for output in selected_outputs:
                if isinstance(output, Mapping):
                    _collect_workspace_artifact_descriptor(
                        descriptors,
                        output.get("artifact"),
                    )
    return descriptors


def _collect_workspace_artifact_descriptor(
    descriptors: dict[str, Mapping[str, object]],
    value: object,
) -> None:
    if not isinstance(value, Mapping):
        return
    relative_path = value.get("relative_path")
    sha256 = value.get("sha256")
    size_bytes = value.get("size_bytes")
    if (
        isinstance(relative_path, str)
        and isinstance(sha256, str)
        and is_strict_int(size_bytes)
    ):
        descriptors[relative_path] = value


def _validate_workspace_artifact_payload(
    node_id: str,
    relative_path: str,
    payload: bytes,
    expected_artifacts: dict[str, Mapping[str, object]],
) -> None:
    descriptor = expected_artifacts.get(relative_path)
    if descriptor is None:
        raise ValueError(
            f"Workspace resume artifact for node '{node_id}' is not reusable."
        )
    if _validate_workspace_state_resume_payload(
        node_id,
        relative_path,
        payload,
        descriptor,
    ):
        return
    if hashlib.sha256(payload).hexdigest() != descriptor["sha256"]:
        raise ValueError(
            f"Workspace resume artifact hash changed for node '{node_id}'."
        )
    if len(payload) != descriptor["size_bytes"]:
        raise ValueError(
            f"Workspace resume artifact size changed for node '{node_id}'."
        )


def _validate_workspace_state_resume_payload(
    node_id: str,
    relative_path: str,
    payload: bytes,
    descriptor: Mapping[str, object],
) -> bool:
    resume_sha256 = descriptor.get("resume_sha256")
    resume_size_bytes = descriptor.get("resume_size_bytes")
    if (
        not Path(relative_path).name.startswith("workspace-state")
        or not isinstance(resume_sha256, str)
        or not is_strict_int(resume_size_bytes)
    ):
        return False
    try:
        state = json.loads(payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    resume_bytes = json.dumps(
        _without_branch_export(state),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if hashlib.sha256(resume_bytes).hexdigest() != resume_sha256:
        raise ValueError(
            f"Workspace resume artifact hash changed for node '{node_id}'."
        )
    if len(resume_bytes) != resume_size_bytes:
        raise ValueError(
            f"Workspace resume artifact size changed for node '{node_id}'."
        )
    return True


def _without_branch_export(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            key: _without_branch_export(item)
            for key, item in value.items()
            if key != "branch_export"
        }
    if isinstance(value, list):
        return [_without_branch_export(item) for item in value]
    return value


def _hydrated_workspace_state_payload(
    payload: bytes,
    frontier: ValidatedResumeFrontier,
    output: ArtifactStorePort,
    node_id: str,
    hydrated_at: str,
) -> bytes:
    try:
        state = json.loads(payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(
            f"Workspace state for node '{node_id}' is not reusable."
        ) from exc
    if not isinstance(state, dict):
        raise ValueError(f"Workspace state for node '{node_id}' is not reusable.")
    state = _without_branch_export(state)
    if not isinstance(state, dict):
        raise ValueError(f"Workspace state for node '{node_id}' is not reusable.")
    state["resume_origin"] = _workspace_state_resume_origin(
        state,
        frontier,
        node_id,
        hydrated_at,
    )
    _scrub_workspace_state_placement(state)
    state["run_id"] = output.run_id
    state["run_key_name"] = output.run_key_name
    state["updated_at"] = hydrated_at
    return json.dumps(state, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def _workspace_state_resume_origin(
    state: dict[str, object],
    frontier: ValidatedResumeFrontier,
    node_id: str,
    hydrated_at: str,
) -> JsonObject:
    origin: JsonObject = {
        "source_run_id": frontier.source.manifest.run_id,
        "source_run_key_name": frontier.source.manifest.run_key_name,
        "source_node_id": node_id,
        "hydrated_at": hydrated_at,
    }
    workspace = state.get("workspace")
    if isinstance(workspace, Mapping):
        origin["source_workspace"] = _json_object(workspace)
    execution = state.get("execution")
    if isinstance(execution, Mapping):
        origin["source_execution"] = _json_object(execution)
    return origin


def _scrub_workspace_state_placement(state: dict[str, object]) -> None:
    workspace = state.get("workspace")
    if isinstance(workspace, dict):
        workspace["path"] = None
        workspace["effective_cwd"] = None
        workspace["cache_root"] = None
        workspace["checkout_root"] = None
        workspace["cache_key"] = None
        workspace["retention"] = "not_applicable"
        workspace["retained_reason"] = "hydrated_resume"
    execution = state.get("execution")
    if isinstance(execution, dict):
        execution["cache_root"] = None
        execution["workspace_path"] = None
        execution["checkout_root"] = None
        execution["effective_cwd"] = None


def _json_object(value: Mapping[str, object]) -> JsonObject:
    result: JsonObject = {}
    for key, item in value.items():
        converted = _json_value(item)
        if converted is not None or item is None:
            result[str(key)] = converted
    return result


def _json_value(value: object) -> JsonValue | None:
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, Mapping):
        return _json_object(value)
    if isinstance(value, list):
        items: list[JsonValue] = []
        for item in value:
            converted = _json_value(item)
            if converted is not None or item is None:
                items.append(converted)
        return items
    return None


def _hydrated_node_state(
    node: PreflightExecutionNode,
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    descriptors: list[ArtifactDescriptor],
    generated_files: list[ArtifactDescriptor],
    frontier: ValidatedResumeFrontier,
    hydrated_at: str,
) -> NodeState:
    return NodeState(
        run_state_schema_version=RUN_STATE_SCHEMA_VERSION,
        plan_schema_version=plan.plan_schema_version,
        workflow_identity=frontier.source.manifest.workflow_identity,
        workflow_name=plan.workflow_name,
        workflow_signature=plan.workflow_signature,
        run_id=output.run_id,
        run_key_name=output.run_key_name,
        node_id=node.id,
        completed_at=hydrated_at,
        artifacts=descriptors,
        generated_files=generated_files,
        workspace=build_node_workspace_descriptor(node, plan, output),
        resume_origin=ResumeOrigin(
            source_run_id=frontier.source.manifest.run_id,
            source_run_key_name=frontier.source.manifest.run_key_name,
            source_node_id=node.id,
            hydrated_at=hydrated_at,
        ),
    )


def _resume_source_payload(
    frontier: ValidatedResumeFrontier,
    node_id: str,
    descriptors: list[ArtifactDescriptor],
    restored_at: str,
) -> JsonObject:
    descriptors_by_kind = {descriptor.kind: descriptor for descriptor in descriptors}
    output_descriptor = descriptors_by_kind.get("output")
    if output_descriptor is None:
        raise ValueError(f"Hydrated resume descriptor missing for node '{node_id}'.")
    payload: JsonObject = {
        "source_run_id": frontier.source.manifest.run_id,
        "source_run_key_name": frontier.source.manifest.run_key_name,
        "source_node_id": node_id,
        "restored_at": restored_at,
        "result_sha256": output_descriptor.sha256,
    }
    findings_descriptor = descriptors_by_kind.get("findings")
    if findings_descriptor is not None:
        payload["findings_sha256"] = findings_descriptor.sha256
    return payload
