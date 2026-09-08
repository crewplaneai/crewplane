from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from crewplane.architecture.safe_files import contained_regular_file
from crewplane.core.preflight.models import PreflightExecutionNode
from crewplane.core.workflow.keywords import ProviderRole

from ...results.review_loop_status import (
    ReviewLoopStatusEntry,
    resolve_review_loop_status,
    task_specs_for_producers,
)
from ...run_history import RunHistoryRecord
from .fields import int_field, nullable_int_field
from .fields import mapping_value as _mapping
from .lineage import (
    INVALID_LINEAGE_ORDER,
    invocation_round_order,
    review_output_coordinates,
)
from .paths import workspace_state_candidates
from .ref_contracts import is_discarded_lineage


class WorkspaceStateStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class ExpectedWorkspaceInvocation:
    task_id: str
    role: ProviderRole
    round_num: int
    audit_round_num: int | None
    lineage_source_required: bool = False


def workspace_state_file(
    source: RunHistoryRecord,
    node: PreflightExecutionNode,
) -> Path | None:
    stage_path = node.artifact_contract.stage_path
    if stage_path is None:
        return None
    return contained_regular_file(source.run_dir, f"{stage_path}/workspace-state.json")


def workspace_state_payloads(
    source: RunHistoryRecord,
    node: PreflightExecutionNode,
) -> tuple[dict[str, object], ...]:
    return workspace_state_payloads_for_status(
        source,
        node,
        WorkspaceStateStatus.SUCCEEDED,
    )


def failed_workspace_state_payloads(
    source: RunHistoryRecord,
    node: PreflightExecutionNode,
) -> tuple[dict[str, object], ...]:
    return workspace_state_payloads_for_status(
        source,
        node,
        WorkspaceStateStatus.FAILED,
    )


def workspace_state_payloads_for_status(
    source: RunHistoryRecord,
    node: PreflightExecutionNode,
    status: WorkspaceStateStatus,
) -> tuple[dict[str, object], ...]:
    stage_path = node.artifact_contract.stage_path
    if stage_path is None:
        return ()
    stage_dir = source.run_dir / stage_path
    payloads: list[dict[str, object]] = []
    for candidate in workspace_state_candidates(stage_dir):
        safe_path = contained_regular_file(
            source.run_dir,
            candidate.relative_to(source.run_dir).as_posix(),
        )
        if safe_path is None:
            continue
        try:
            payload = json.loads(safe_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            return ()
        if not isinstance(payload, dict):
            return ()
        if payload.get("status") == status:
            payloads.append(payload)
    return tuple(payloads)


def expected_workspace_invocations(
    source: RunHistoryRecord,
    node: PreflightExecutionNode,
) -> tuple[ExpectedWorkspaceInvocation, ...]:
    stage_path = node.artifact_contract.stage_path
    if stage_path is None:
        return ()
    stage_dir = source.run_dir / stage_path
    try:
        resolved_review_status = resolve_review_loop_status(
            node.id,
            stage_dir,
            task_specs_for_producers(node.provider_records),
        )
    except RuntimeError:
        return ()
    if resolved_review_status is not None:
        expected = [
            *(
                expected_review_status_invocation(entry, lineage_source_required=True)
                for entry in resolved_review_status.canonical_executor_outputs
            ),
            *(
                expected_review_status_invocation(entry)
                for entry in resolved_review_status.reviewer_outputs
            ),
        ]
        if any(invocation is None for invocation in expected):
            return ()
        return tuple(invocation for invocation in expected if invocation is not None)
    if node.mode == "parallel":
        return _parallel_workspace_invocations_with_status(
            source,
            node,
            WorkspaceStateStatus.SUCCEEDED,
            lineage_source_required=True,
        )
    if node.mode != "sequential" or len(node.provider_records) != 1:
        return ()
    provider = node.provider_records[0]
    if provider.role != ProviderRole.EXECUTOR:
        return ()
    return (
        ExpectedWorkspaceInvocation(
            task_id=provider.task_id,
            role=ProviderRole.EXECUTOR,
            round_num=node.execution_policy.depth or 1,
            audit_round_num=None,
            lineage_source_required=True,
        ),
    )


def expected_failed_workspace_invocations(
    source: RunHistoryRecord,
    node: PreflightExecutionNode,
) -> tuple[ExpectedWorkspaceInvocation, ...]:
    if node.mode == "sequential":
        return _failed_sequential_workspace_invocations(source, node)
    if node.mode != "parallel":
        return ()
    return _parallel_workspace_invocations_with_status(
        source,
        node,
        WorkspaceStateStatus.FAILED,
        lineage_source_required=False,
    )


def _failed_sequential_workspace_invocations(
    source: RunHistoryRecord,
    node: PreflightExecutionNode,
) -> tuple[ExpectedWorkspaceInvocation, ...]:
    providers = {provider.task_id: provider for provider in node.provider_records}
    expected: list[ExpectedWorkspaceInvocation] = []
    for payload in failed_workspace_state_payloads(source, node):
        task_id = payload.get("task_id")
        round_num = int_field(payload, "round_num")
        audit_round_num = nullable_int_field(payload, "audit_round_num")
        provider = providers.get(task_id) if isinstance(task_id, str) else None
        if (
            provider is None
            or payload.get("provider") != provider.provider
            or payload.get("role") != provider.role
            or round_num is None
            or not audit_round_num.valid
        ):
            return ()
        if (
            provider.role == ProviderRole.REVIEWER
            and not node.execution_policy.continue_on_failure
        ):
            return ()
        if provider.role == ProviderRole.EXECUTOR and not is_discarded_lineage(payload):
            return ()
        expected.append(
            ExpectedWorkspaceInvocation(
                task_id=provider.task_id,
                role=provider.role,
                round_num=round_num,
                audit_round_num=audit_round_num.value,
            )
        )
    return tuple(expected)


def _parallel_workspace_invocations_with_status(
    source: RunHistoryRecord,
    node: PreflightExecutionNode,
    status: WorkspaceStateStatus,
    lineage_source_required: bool,
) -> tuple[ExpectedWorkspaceInvocation, ...]:
    payloads = workspace_state_payloads_for_status(source, node, status)
    expected: list[ExpectedWorkspaceInvocation] = []
    for provider in node.provider_records:
        if provider.role != ProviderRole.EXECUTOR:
            continue
        invocation = ExpectedWorkspaceInvocation(
            task_id=provider.task_id,
            role=ProviderRole.EXECUTOR,
            round_num=1,
            audit_round_num=None,
            lineage_source_required=lineage_source_required,
        )
        if _payloads_contain_invocation(payloads, invocation):
            expected.append(invocation)
    return tuple(expected)


def _payloads_contain_invocation(
    payloads: tuple[dict[str, object], ...],
    expected: ExpectedWorkspaceInvocation,
) -> bool:
    return any(
        payload_matches_expected_invocation(payload, expected) for payload in payloads
    )


def expected_review_status_invocation(
    entry: ReviewLoopStatusEntry,
    lineage_source_required: bool = False,
) -> ExpectedWorkspaceInvocation | None:
    coordinates = review_output_coordinates(entry.relative_path, entry.task_id)
    if coordinates is None:
        return None
    return ExpectedWorkspaceInvocation(
        task_id=coordinates.task_id,
        role=entry.role,
        round_num=coordinates.round_num,
        audit_round_num=coordinates.audit_round_num,
        lineage_source_required=lineage_source_required,
    )


def payload_matches_expected_invocation(
    payload: dict[str, object],
    expected: ExpectedWorkspaceInvocation,
) -> bool:
    round_num = int_field(payload, "round_num")
    if round_num is None:
        return False
    audit_round_num = nullable_int_field(payload, "audit_round_num")
    if not audit_round_num.valid:
        return False
    return (
        payload.get("task_id") == expected.task_id
        and payload.get("role") == expected.role
        and round_num == expected.round_num
        and audit_round_num.value == expected.audit_round_num
    )


def resolve_expected_workspace_payload(
    payloads: tuple[dict[str, object], ...],
    expected: ExpectedWorkspaceInvocation,
) -> dict[str, object] | None:
    matches = [
        payload
        for payload in payloads
        if payload_matches_expected_invocation(payload, expected)
    ]
    if len(matches) == 1:
        return matches[0]
    if matches or not expected_seeded_lineage_invocation(expected):
        return None
    return latest_lineage_payload_before(payloads, expected)


def expected_seeded_lineage_invocation(
    expected: ExpectedWorkspaceInvocation,
) -> bool:
    return (
        expected.lineage_source_required
        and expected.role == ProviderRole.EXECUTOR
        and expected.audit_round_num is not None
        and expected.audit_round_num > 1
        and expected.round_num == 1
    )


def latest_lineage_payload_before(
    payloads: tuple[dict[str, object], ...],
    expected: ExpectedWorkspaceInvocation,
) -> dict[str, object] | None:
    before = (expected.audit_round_num or 0, expected.round_num)
    candidates = [
        payload
        for payload in payloads
        if payload.get("task_id") == expected.task_id
        and lineage_payload_order(payload) < before
    ]
    candidates = [
        payload for payload in candidates if lineage_payload_order(payload) >= (0, 0)
    ]
    if not candidates:
        return None
    candidates.sort(key=lineage_payload_order)
    return candidates[-1]


def lineage_payload_order(payload: dict[str, object]) -> tuple[int, int]:
    workspace = _mapping(payload.get("workspace"))
    if not (
        payload.get("role") == ProviderRole.EXECUTOR
        and workspace.get("lineage_producer") is True
    ):
        return (-1, -1)
    return invocation_round_order(payload) or INVALID_LINEAGE_ORDER
