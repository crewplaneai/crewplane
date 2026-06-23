from __future__ import annotations

from pathlib import Path

from crewplane.core.preflight.models import (
    PreflightExecutionNode,
    PreflightExecutionPlan,
)

from ..results.review_loop_status import resolve_review_loop_status
from ..results.selection import parse_audit_round, parse_task_round
from ..run_history import RunHistoryRecord
from ..safe_files import contained_regular_file
from .state.fields import int_field, nullable_int_field
from .state.invocations import workspace_state_payloads


def workspace_invocation_source_matches(
    run: RunHistoryRecord,
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    payload: dict[str, object],
) -> bool:
    descriptor = _normalized_source_descriptor(payload)
    if descriptor is None:
        return False
    kind = descriptor["kind"]
    if kind == "project":
        return _project_source_matches(plan, node, payload, descriptor)
    if kind == "node":
        return _node_source_matches(run, plan, node, payload, descriptor)
    if kind == "candidate":
        return _candidate_source_matches(run, plan, node, payload, descriptor)
    return False


def _normalized_source_descriptor(
    payload: dict[str, object],
) -> dict[str, object] | None:
    source = _mapping(payload.get("source"))
    invocation_source = _mapping(payload.get("invocation_source"))
    descriptor = {
        "kind": source.get("kind"),
        "node_id": source.get("node_id"),
        "commit": source.get("commit"),
        "tree": source.get("tree"),
        "candidate_sequence": source.get("candidate_sequence"),
        "bundle_path": source.get("bundle_path"),
        "bundle_sha256": source.get("bundle_sha256"),
        "bundle_size_bytes": source.get("bundle_size_bytes"),
        "bundle_ref": source.get("bundle_ref"),
    }
    alternate = {
        "kind": invocation_source.get("source_kind"),
        "node_id": invocation_source.get("source_node_id"),
        "commit": invocation_source.get("source_commit"),
        "tree": invocation_source.get("source_tree"),
        "candidate_sequence": invocation_source.get("candidate_sequence"),
        "bundle_path": invocation_source.get("source_bundle_path"),
        "bundle_sha256": invocation_source.get("source_bundle_sha256"),
        "bundle_size_bytes": invocation_source.get("source_bundle_size_bytes"),
        "bundle_ref": invocation_source.get("source_bundle_ref"),
    }
    if descriptor != alternate:
        return None
    return descriptor


def _project_source_matches(
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    payload: dict[str, object],
    descriptor: dict[str, object],
) -> bool:
    workspace_source = plan.workspace_source
    policy = node.workspace_policy
    if workspace_source is None or policy is None or policy.source_kind != "project":
        return False
    if policy.lineage_producer and not _initial_executor_payload(payload):
        return False
    return (
        descriptor.get("node_id") is None
        and descriptor.get("commit") == workspace_source.run_base_commit
        and descriptor.get("tree") == workspace_source.source_tree
        and descriptor.get("candidate_sequence") is None
    )


def _node_source_matches(
    run: RunHistoryRecord,
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    payload: dict[str, object],
    descriptor: dict[str, object],
) -> bool:
    policy = node.workspace_policy
    if (
        policy is None
        or policy.source_kind != "node"
        or policy.source_node_id is None
        or not _initial_executor_payload(payload)
    ):
        return False
    if descriptor.get("node_id") != policy.source_node_id:
        return False
    return _descriptor_matches_lineage_result(
        run,
        plan,
        policy.source_node_id,
        descriptor,
    )


def _candidate_source_matches(
    run: RunHistoryRecord,
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    payload: dict[str, object],
    descriptor: dict[str, object],
) -> bool:
    if descriptor.get("node_id") != node.id:
        return False
    round_num = int_field(payload, "round_num")
    if round_num is None:
        return False
    audit_round_num = nullable_int_field(payload, "audit_round_num")
    if not audit_round_num.valid:
        return False
    if payload.get("role") == "reviewer":
        source_round = round_num
    elif payload.get("role") == "executor" and round_num > 1:
        source_round = round_num - 1
    else:
        return False
    return _descriptor_matches_lineage_result(
        run,
        plan,
        node.id,
        descriptor,
        round_num=source_round,
        audit_round_num=audit_round_num.value,
    )


def _descriptor_matches_lineage_result(
    run: RunHistoryRecord,
    plan: PreflightExecutionPlan,
    node_id: str,
    descriptor: dict[str, object],
    round_num: int | None = None,
    audit_round_num: int | None = None,
) -> bool:
    node = _node_by_id(plan, node_id)
    if node is None:
        return False
    payloads = workspace_state_payloads(run, node)
    if round_num is None and audit_round_num is None:
        canonical_payload = _canonical_lineage_payload(run, node, payloads)
        if canonical_payload is not None:
            return _descriptor_matches_result(descriptor, canonical_payload)
        return False
    found_exact_source = False
    for payload in payloads:
        if not _lineage_payload_matches(payload, round_num, audit_round_num):
            continue
        found_exact_source = True
        if _descriptor_matches_result(descriptor, payload):
            return True
    if found_exact_source:
        return False
    seeded_source = _seeded_audit_source_payload(
        payloads,
        round_num,
        audit_round_num,
    )
    if seeded_source is not None:
        return _descriptor_matches_result(descriptor, seeded_source)
    return False


def _descriptor_matches_result(
    descriptor: dict[str, object],
    payload: dict[str, object],
) -> bool:
    result = _mapping(payload.get("result"))
    return (
        descriptor.get("commit") == result.get("result_commit")
        and descriptor.get("tree") == result.get("result_tree")
        and descriptor.get("candidate_sequence") == 1
        and _descriptor_bundle_matches_result(descriptor, payload)
    )


def _descriptor_bundle_matches_result(
    descriptor: dict[str, object],
    payload: dict[str, object],
) -> bool:
    if descriptor.get("kind") not in {"node", "candidate"}:
        return True
    bundle = _mapping(payload.get("bundle"))
    return (
        descriptor.get("bundle_path") == bundle.get("path")
        and isinstance(descriptor.get("bundle_sha256"), str)
        and descriptor.get("bundle_sha256") == bundle.get("sha256")
        and descriptor.get("bundle_size_bytes") == bundle.get("size_bytes")
        and descriptor.get("bundle_ref") == _result_ref(payload)
    )


def _result_ref(payload: dict[str, object]) -> str | None:
    refs = _mapping(payload.get("refs"))
    ref = refs.get("result")
    return ref if isinstance(ref, str) else None


def _lineage_payload_matches(
    payload: dict[str, object],
    round_num: int | None,
    audit_round_num: int | None,
) -> bool:
    workspace = _mapping(payload.get("workspace"))
    if not (
        payload.get("role") == "executor" and workspace.get("lineage_producer") is True
    ):
        return False
    payload_round_num = int_field(payload, "round_num")
    if payload_round_num is None:
        return False
    payload_audit_round_num = nullable_int_field(payload, "audit_round_num")
    if not payload_audit_round_num.valid:
        return False
    if round_num is not None and payload_round_num != round_num:
        return False
    return payload_audit_round_num.value == audit_round_num


def _initial_executor_payload(payload: dict[str, object]) -> bool:
    return payload.get("role") == "executor" and int_field(payload, "round_num") == 1


def _seeded_audit_source_payload(
    payloads: tuple[dict[str, object], ...],
    round_num: int | None,
    audit_round_num: int | None,
) -> dict[str, object] | None:
    if round_num != 1 or audit_round_num is None or audit_round_num <= 1:
        return None
    prior_payloads = [
        payload
        for payload in payloads
        if _lineage_payload_is_ordered_source(payload)
        and _lineage_payload_order(payload) < (audit_round_num, round_num)
    ]
    if not prior_payloads:
        return None
    prior_payloads.sort(key=_lineage_payload_order)
    return prior_payloads[-1]


def _canonical_lineage_payload(
    run: RunHistoryRecord,
    node: PreflightExecutionNode,
    payloads: tuple[dict[str, object], ...],
) -> dict[str, object] | None:
    stage_path = node.artifact_contract.stage_path
    if stage_path is None:
        return None
    stage_dir = run.run_dir / stage_path
    relative_status_path = f"{stage_path}/review-state/review-loop-status.json"
    safe_status_path = contained_regular_file(run.run_dir, relative_status_path)
    if safe_status_path is not None:
        return _review_loop_canonical_payload(stage_dir, node.id, payloads)
    if (stage_dir / "review-state" / "review-loop-status.json").exists():
        return None
    return _latest_lineage_payload(payloads)


def _review_loop_canonical_payload(
    stage_dir: Path,
    node_id: str,
    payloads: tuple[dict[str, object], ...],
) -> dict[str, object] | None:
    try:
        resolved = resolve_review_loop_status(node_id, stage_dir)
    except RuntimeError:
        return None
    if resolved is None or len(resolved.canonical_executor_outputs) != 1:
        return None
    entry = resolved.canonical_executor_outputs[0]
    task_id, round_num = parse_task_round(entry.output_file.stem)
    if task_id != entry.task_id or round_num <= 0:
        return None
    audit_round_num = None
    relative_path = entry.relative_path
    path_parts = relative_path.split("/")
    if len(path_parts) > 1:
        parsed_audit_round = parse_audit_round(path_parts[0])
        audit_round_num = parsed_audit_round if parsed_audit_round > 0 else None
    exact = _lineage_payload_by_invocation(
        payloads,
        entry.task_id,
        round_num,
        audit_round_num,
    )
    if exact is not None:
        return exact
    if audit_round_num is not None and audit_round_num > 1 and round_num == 1:
        return _latest_lineage_payload(payloads, before=(audit_round_num, round_num))
    return None


def _lineage_payload_by_invocation(
    payloads: tuple[dict[str, object], ...],
    task_id: str,
    round_num: int,
    audit_round_num: int | None,
) -> dict[str, object] | None:
    matches = [
        payload
        for payload in payloads
        if payload.get("task_id") == task_id
        and _lineage_payload_matches(payload, round_num, audit_round_num)
    ]
    return matches[0] if len(matches) == 1 else None


def _latest_lineage_payload(
    payloads: tuple[dict[str, object], ...],
    before: tuple[int, int] | None = None,
) -> dict[str, object] | None:
    lineage_payloads = [
        payload
        for payload in payloads
        if _lineage_payload_is_ordered_source(payload)
        and (before is None or _lineage_payload_order(payload) < before)
    ]
    if not lineage_payloads:
        return None
    lineage_payloads.sort(key=_lineage_payload_order)
    return lineage_payloads[-1]


def _lineage_payload_order(payload: dict[str, object]) -> tuple[int, int]:
    round_num = int_field(payload, "round_num")
    if round_num is None:
        return (-1, -1)
    audit_round_num = nullable_int_field(payload, "audit_round_num")
    if not audit_round_num.valid:
        return (-1, -1)
    if audit_round_num.value is None:
        return (0, round_num)
    return (audit_round_num.value, round_num)


def _lineage_payload_is_ordered_source(payload: dict[str, object]) -> bool:
    order = _lineage_payload_order(payload)
    audit_round_num = nullable_int_field(payload, "audit_round_num")
    if not audit_round_num.valid:
        return False
    return order >= (0, 0) and _lineage_payload_matches(
        payload,
        round_num=None,
        audit_round_num=audit_round_num.value,
    )


def _node_by_id(
    plan: PreflightExecutionPlan,
    node_id: str,
) -> PreflightExecutionNode | None:
    for node in plan.nodes:
        if node.id == node_id:
            return node
    return None


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}
