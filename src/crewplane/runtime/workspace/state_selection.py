from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from crewplane.architecture.ports import ArtifactStorePort
from crewplane.artifacts.results.review_loop_status import (
    ReviewLoopStatusEntry,
    resolve_review_loop_status,
)
from crewplane.artifacts.results.selection import (
    parse_audit_round,
    parse_task_round,
)
from crewplane.artifacts.safe_files import contained_regular_file
from crewplane.core.preflight.models import PreflightExecutionNode


@dataclass(frozen=True)
class WorkspaceStateInvocation:
    task_id: str
    round_num: int
    audit_round_num: int | None


def required_lineage_state_path(output: ArtifactStorePort, node_id: str) -> Path:
    stage_dir = output.get_stage_dir(node_id)
    if stage_dir is None:
        raise RuntimeError(
            f"Workspace lineage source '{node_id}' has no stage directory."
        )
    review_state = review_loop_canonical_lineage_state_path(stage_dir, node_id)
    if review_state is not None:
        return review_state
    latest = latest_executor_lineage_state_path(stage_dir)
    if latest is not None:
        return latest
    canonical = safe_workspace_state_path(stage_dir, "workspace-state.json")
    if canonical is not None and workspace_state_is_lineage_source(canonical):
        return canonical
    raise RuntimeError(
        f"Workspace lineage source '{node_id}' has no succeeded workspace state."
    )


def same_node_executor_state_path(
    output: ArtifactStorePort,
    node: PreflightExecutionNode,
    round_num: int,
    audit_round_num: int | None,
    allow_prior_fallback: bool = False,
) -> Path | None:
    stage_dir = output.get_stage_dir(node.id)
    if stage_dir is None:
        return None
    task_ids = {
        provider.task_id
        for provider in node.provider_records
        if provider.role == "executor"
    }
    match = WorkspaceStateInvocation(
        task_id=next(iter(task_ids)) if len(task_ids) == 1 else "",
        round_num=round_num,
        audit_round_num=audit_round_num,
    )
    exact = find_lineage_state_path(stage_dir, match, task_ids)
    if exact is not None:
        return exact
    if is_seeded_audit_round(match) or allow_prior_fallback:
        return latest_executor_lineage_state_path(
            stage_dir,
            task_ids,
            before=state_invocation_order(match),
        )
    return None


def latest_executor_lineage_state_path(
    stage_dir: Path,
    task_ids: set[str] | None = None,
    before: tuple[int, int] | None = None,
) -> Path | None:
    states = sorted(
        iter_lineage_states(stage_dir, task_ids, before),
        key=lambda item: item[0],
    )
    if not states:
        return None
    latest_order = states[-1][0]
    latest_states = [path for order, path, _ in states if order == latest_order]
    if len(latest_states) > 1:
        paths = ", ".join(path.name for path in latest_states)
        raise RuntimeError(f"Ambiguous executor workspace states: {paths}.")
    return latest_states[0]


def find_lineage_state_path(
    stage_dir: Path,
    match: WorkspaceStateInvocation,
    task_ids: set[str] | None = None,
) -> Path | None:
    matches = [
        path
        for _, path, payload in iter_lineage_states(stage_dir, task_ids)
        if payload_matches_invocation(payload, match)
    ]
    if len(matches) > 1:
        paths = ", ".join(path.name for path in matches)
        raise RuntimeError(f"Ambiguous executor workspace states: {paths}.")
    return matches[0] if matches else None


def review_loop_canonical_lineage_state_path(
    stage_dir: Path,
    node_id: str,
) -> Path | None:
    resolved = resolve_review_loop_status(node_id, stage_dir)
    if resolved is None:
        return None
    canonical_outputs = resolved.canonical_executor_outputs
    if len(canonical_outputs) != 1:
        raise RuntimeError(
            f"Workspace lineage source '{node_id}' must have exactly one "
            f"canonical executor output, found {len(canonical_outputs)}."
        )
    invocation = invocation_from_review_status(canonical_outputs[0])
    exact = find_lineage_state_path(stage_dir, invocation, {invocation.task_id})
    if exact is not None:
        return exact
    if is_seeded_audit_round(invocation):
        seeded_source = latest_executor_lineage_state_path(
            stage_dir,
            {invocation.task_id},
            before=state_invocation_order(invocation),
        )
        if seeded_source is not None:
            return seeded_source
    raise RuntimeError(
        f"Workspace lineage source '{node_id}' canonical executor output has no "
        "matching succeeded workspace state."
    )


def invocation_from_review_status(
    entry: ReviewLoopStatusEntry,
) -> WorkspaceStateInvocation:
    relative_path = Path(entry.relative_path)
    task_id, round_num = parse_task_round(relative_path.stem)
    if task_id != entry.task_id or round_num <= 0:
        raise RuntimeError(
            "Workspace review-loop status points to an executor output that does "
            f"not match its task id: {entry.relative_path}."
        )
    audit_round_num = None
    if len(relative_path.parts) > 1:
        audit_round = parse_audit_round(relative_path.parts[0])
        audit_round_num = audit_round if audit_round > 0 else None
    return WorkspaceStateInvocation(
        task_id=entry.task_id,
        round_num=round_num,
        audit_round_num=audit_round_num,
    )


def iter_lineage_states(
    stage_dir: Path,
    task_ids: set[str] | None = None,
    before: tuple[int, int] | None = None,
) -> list[tuple[tuple[int, int], Path, dict[str, object]]]:
    states: list[tuple[tuple[int, int], Path, dict[str, object]]] = []
    for path in workspace_state_paths(stage_dir):
        payload = read_workspace_state(path)
        if not workspace_state_payload_is_lineage_source(payload):
            continue
        if task_ids is not None and payload.get("task_id") not in task_ids:
            continue
        order = payload_order(payload)
        if order is None:
            continue
        if before is not None and order >= before:
            continue
        states.append((order, path, payload))
    return states


def workspace_state_paths(stage_dir: Path) -> tuple[Path, ...]:
    names = ["workspace-state.json"]
    names.extend(path.name for path in sorted(stage_dir.glob("workspace-state-*.json")))
    paths = [
        path
        for name in dict.fromkeys(names)
        if (path := safe_workspace_state_path(stage_dir, name)) is not None
    ]
    return tuple(paths)


def safe_workspace_state_path(stage_dir: Path, file_name: str) -> Path | None:
    candidate = stage_dir / file_name
    safe_path = contained_regular_file(stage_dir, file_name)
    if safe_path is not None:
        return safe_path
    if candidate.exists() or candidate.is_symlink():
        raise RuntimeError(f"Unsafe workspace state artifact: {candidate.as_posix()}")
    return None


def workspace_state_is_lineage_source(path: Path) -> bool:
    return workspace_state_payload_is_lineage_source(read_workspace_state(path))


def workspace_state_payload_is_lineage_source(payload: dict[str, object]) -> bool:
    workspace = payload.get("workspace")
    result = payload.get("result")
    return (
        payload.get("status") == "succeeded"
        and payload.get("role") == "executor"
        and isinstance(workspace, dict)
        and workspace.get("lineage_producer") is True
        and isinstance(result, dict)
        and isinstance(result.get("result_commit"), str)
        and isinstance(result.get("result_tree"), str)
    )


def read_workspace_state(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def payload_matches_invocation(
    payload: dict[str, object],
    invocation: WorkspaceStateInvocation,
) -> bool:
    return (
        payload.get("task_id") == invocation.task_id
        and payload.get("round_num") == invocation.round_num
        and payload.get("audit_round_num") == invocation.audit_round_num
    )


def payload_order(payload: dict[str, object]) -> tuple[int, int] | None:
    round_num = payload.get("round_num")
    audit_round_num = payload.get("audit_round_num")
    if isinstance(round_num, bool) or not isinstance(round_num, int):
        return None
    if audit_round_num is None:
        return (0, round_num)
    if isinstance(audit_round_num, bool) or not isinstance(audit_round_num, int):
        return None
    return (audit_round_num, round_num)


def state_invocation_order(invocation: WorkspaceStateInvocation) -> tuple[int, int]:
    audit_round_num = invocation.audit_round_num or 0
    return (audit_round_num, invocation.round_num)


def is_seeded_audit_round(invocation: WorkspaceStateInvocation) -> bool:
    return (
        invocation.audit_round_num is not None
        and invocation.audit_round_num > 1
        and invocation.round_num == 1
    )
