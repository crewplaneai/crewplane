from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from orchestrator_cli.architecture.contracts import JsonObject, JsonValue
from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.core.execution_state import NodeState
from orchestrator_cli.core.preflight.models import (
    PreflightExecutionNode,
    PreflightExecutionPlan,
)
from orchestrator_cli.core.preflight.workspace.observability import (
    invoker_workspace_descriptor,
    node_result_descriptor,
)

from ..atomic import atomic_write_json
from ..naming import build_node_state_filename
from ..results.review_loop_status import (
    REVIEW_LOOP_STATUS_RELATIVE_PATH,
    ReviewLoopStatusEntry,
    resolve_review_loop_status,
)
from ..safe_files import contained_regular_file


def build_node_workspace_descriptor(
    node: PreflightExecutionNode,
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
) -> JsonObject | None:
    policy = node.workspace_policy
    if policy is None or not policy.enabled:
        return None
    stage_dir = output.get_stage_dir(node.id)
    if stage_dir is None:
        raise RuntimeError(
            f"Workspace-enabled node '{node.id}' has no stage directory."
        )
    state_paths = _workspace_state_paths(stage_dir)
    if not state_paths:
        raise RuntimeError(
            f"Workspace-enabled node '{node.id}' has no workspace-state artifact."
        )
    descriptor: JsonObject = {
        "enabled": True,
        "node_id": node.id,
        "mode": node.mode,
        "worktree_contract": policy.worktree_contract.model_dump(mode="json"),
        "logical_worktree_name": policy.logical_worktree_name,
        "kind": policy.declaration_kind,
        "source_kind": policy.source_kind,
        "source_node_id": policy.source_node_id,
        "clean_start": policy.clean_start,
        "materialization": policy.materialization,
        "lineage_producer": policy.lineage_producer,
        "writable": policy.writable,
        "result": node_result_descriptor(node),
        "policy": policy.model_dump(mode="json", exclude_none=True),
        "workspace_file_locator_count": sum(
            locator.node_id == node.id for locator in plan.workspace_file_locators
        ),
        "states": [_state_descriptor(output.stages_dir, path) for path in state_paths],
    }
    review_loop = _review_loop_descriptor(output.stages_dir, stage_dir, node.id)
    if review_loop is not None:
        descriptor["review_loop"] = review_loop
    invoker = invoker_workspace_descriptor(plan.runtime_config_snapshot)
    if invoker is not None:
        descriptor["invoker"] = invoker
    return descriptor


def refresh_node_workspace_descriptor(
    node: PreflightExecutionNode,
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
) -> Path | None:
    node_state_path = (
        output.stages_dir / "manifests" / "nodes" / build_node_state_filename(node.id)
    )
    if not node_state_path.is_file():
        return None
    node_state = NodeState.model_validate_json(
        node_state_path.read_text(encoding="utf-8")
    )
    refreshed = node_state.model_copy(
        update={"workspace": build_node_workspace_descriptor(node, plan, output)}
    )
    validated = NodeState.model_validate(refreshed.model_dump(mode="json"))
    return atomic_write_json(
        node_state_path,
        validated.model_dump(mode="json", exclude_none=True),
    )


def _workspace_state_paths(stage_dir: Path) -> tuple[Path, ...]:
    if not stage_dir.is_dir() or stage_dir.is_symlink():
        return ()
    names = ["workspace-state.json"]
    names.extend(path.name for path in sorted(stage_dir.glob("workspace-state-*.json")))
    paths = [
        path
        for name in names
        if (path := contained_regular_file(stage_dir, name)) is not None
    ]
    return tuple(paths)


def _state_descriptor(stages_dir: Path, state_path: Path) -> JsonObject:
    payload = _read_json_object(state_path)
    descriptor: JsonObject = {
        "workspace_state_artifact": _workspace_state_artifact_descriptor(
            stages_dir,
            state_path,
            payload,
        ),
        "status": _json_value(payload.get("status")),
        "task_id": _json_value(payload.get("task_id")),
        "provider": _json_value(payload.get("provider")),
        "role": _json_value(payload.get("role")),
        "round_num": _json_value(payload.get("round_num")),
        "audit_round_num": _json_value(payload.get("audit_round_num")),
        "workspace_kind": _json_value(payload.get("workspace_kind")),
        "logical_worktree_name": _json_value(payload.get("logical_worktree_name")),
        "clean_start": _json_value(payload.get("clean_start")),
        "worktree_contract": _json_value(payload.get("worktree_contract")),
        "git": _json_value(payload.get("git")),
        "source": _json_value(payload.get("source")),
        "invoker": _json_value(payload.get("invoker")),
        "invocation_source": _invocation_source(payload),
        "workspace": _json_value(payload.get("workspace")),
        "execution": _json_value(payload.get("execution")),
        "child_process_environment": _json_value(
            payload.get("child_process_environment")
        ),
        "result": _json_value(payload.get("result")),
        "refs": _json_value(payload.get("refs")),
        "bundle": _bundle_descriptor(stages_dir, payload),
        "setup": _setup_descriptor(stages_dir, state_path, payload),
        "branch_export": _json_value(payload.get("branch_export")),
        "rendered_workspace_files": _json_value(
            payload.get("rendered_workspace_files")
        ),
        "diagnostics": _json_value(payload.get("diagnostics")),
    }
    resume_origin = payload.get("resume_origin")
    if resume_origin is not None:
        descriptor["resume_origin"] = _json_value(resume_origin)
    return descriptor


def _review_loop_descriptor(
    stages_dir: Path,
    stage_dir: Path,
    node_id: str,
) -> JsonObject | None:
    resolved = resolve_review_loop_status(node_id, stage_dir)
    if resolved is None:
        return None
    status_path = stage_dir / REVIEW_LOOP_STATUS_RELATIVE_PATH
    return {
        "status_artifact": _artifact_descriptor(stages_dir, status_path),
        "selected_outputs": [
            _review_loop_output_descriptor(stages_dir, entry)
            for entry in (
                *resolved.canonical_executor_outputs,
                *resolved.reviewer_outputs,
            )
        ],
    }


def _review_loop_output_descriptor(
    stages_dir: Path,
    entry: ReviewLoopStatusEntry,
) -> JsonObject:
    return {
        "task_id": entry.task_id,
        "provider": entry.provider,
        "role": entry.role,
        "relative_path": entry.relative_path,
        "artifact": _artifact_descriptor(stages_dir, entry.output_file),
    }


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Workspace state is not valid JSON: {path.as_posix()}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Workspace state is not an object: {path.as_posix()}")
    return payload


def _artifact_descriptor(stages_dir: Path, path: Path) -> JsonObject:
    payload = path.read_bytes()
    return {
        "relative_path": path.relative_to(stages_dir).as_posix(),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }


def _workspace_state_artifact_descriptor(
    stages_dir: Path,
    path: Path,
    payload: Mapping[str, object],
) -> JsonObject:
    descriptor = _artifact_descriptor(stages_dir, path)
    resume_payload = _without_branch_export(payload)
    resume_bytes = json.dumps(
        resume_payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    descriptor["resume_sha256"] = hashlib.sha256(resume_bytes).hexdigest()
    descriptor["resume_size_bytes"] = len(resume_bytes)
    return descriptor


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


def _bundle_descriptor(
    stages_dir: Path,
    payload: Mapping[str, object],
) -> JsonObject | None:
    bundle = payload.get("bundle")
    if not isinstance(bundle, Mapping):
        return None
    descriptor = _mapping_json_value(bundle)
    relative_path = bundle.get("path")
    if isinstance(relative_path, str):
        bundle_path = _safe_run_artifact_path(stages_dir, relative_path)
        if bundle_path is None:
            raise RuntimeError(f"Workspace bundle artifact is missing: {relative_path}")
        descriptor["artifact"] = _artifact_descriptor(stages_dir, bundle_path)
    return descriptor


def _setup_descriptor(
    stages_dir: Path,
    state_path: Path,
    payload: Mapping[str, object],
) -> JsonObject | None:
    setup = payload.get("setup")
    if not isinstance(setup, Mapping):
        return None
    descriptor = _mapping_json_value(setup)
    _attach_setup_artifact_descriptor(
        descriptor,
        stages_dir,
        state_path,
        setup,
        "metadata_path",
        "metadata_artifact",
    )
    _attach_setup_artifact_descriptor(
        descriptor,
        stages_dir,
        state_path,
        setup,
        "log_path",
        "log_artifact",
    )
    return descriptor


def _attach_setup_artifact_descriptor(
    descriptor: JsonObject,
    stages_dir: Path,
    state_path: Path,
    setup: Mapping[str, object],
    path_key: str,
    artifact_key: str,
) -> None:
    relative_path = setup.get(path_key)
    if not isinstance(relative_path, str):
        return
    artifact_path = _safe_stage_artifact_path(state_path.parent, relative_path)
    if artifact_path is None:
        raise RuntimeError(f"Workspace setup artifact is missing: {relative_path}")
    descriptor[artifact_key] = _artifact_descriptor(stages_dir, artifact_path)


def _safe_run_artifact_path(stages_dir: Path, relative_path: str) -> Path | None:
    return contained_regular_file(stages_dir, relative_path)


def _safe_stage_artifact_path(stage_dir: Path, relative_path: str) -> Path | None:
    return contained_regular_file(stage_dir, relative_path)


def _invocation_source(payload: Mapping[str, object]) -> JsonValue:
    invocation_source = payload.get("invocation_source")
    if invocation_source is not None:
        return _json_value(invocation_source)
    return _json_value(payload.get("source"))


def _json_value(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        return _mapping_json_value(value)
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def _mapping_json_value(value: Mapping[object, object]) -> JsonObject:
    return {
        str(key): _json_value(item)
        for key, item in value.items()
        if isinstance(key, str)
    }
