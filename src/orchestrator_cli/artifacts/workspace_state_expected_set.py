from __future__ import annotations

from .workspace_state_fields import int_field, nullable_int_field
from .workspace_state_invocations import (
    ExpectedWorkspaceInvocation,
    expected_seeded_lineage_invocation,
    latest_lineage_payload_before,
    lineage_payload_order,
    payload_matches_expected_invocation,
)


def workspace_state_payloads_match_expected_set(
    payloads: tuple[dict[str, object], ...],
    expected_invocations: tuple[ExpectedWorkspaceInvocation, ...],
) -> bool:
    matched_payloads = _matched_expected_payloads(payloads, expected_invocations)
    if matched_payloads is None:
        return False
    allowed_payload_ids = {id(payload) for payload in matched_payloads}
    _add_candidate_source_payloads(payloads, allowed_payload_ids)
    return all(
        id(payload) in allowed_payload_ids or not _payload_produces_lineage(payload)
        for payload in payloads
    )


def _matched_expected_payloads(
    payloads: tuple[dict[str, object], ...],
    expected_invocations: tuple[ExpectedWorkspaceInvocation, ...],
) -> tuple[dict[str, object], ...] | None:
    matched: list[dict[str, object]] = []
    for expected in expected_invocations:
        payload = _matched_expected_payload(payloads, expected)
        if payload is None:
            return None
        matched.append(payload)
    return tuple(matched)


def _matched_expected_payload(
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


def _add_candidate_source_payloads(
    payloads: tuple[dict[str, object], ...],
    allowed_payload_ids: set[int],
) -> None:
    pending = [payload for payload in payloads if id(payload) in allowed_payload_ids]
    processed_payload_ids: set[int] = set()

    while pending:
        payload = pending.pop()
        payload_id = id(payload)
        if payload_id in processed_payload_ids:
            continue
        processed_payload_ids.add(payload_id)

        source_payload = _candidate_source_payload(payloads, payload)
        if source_payload is None:
            continue
        source_payload_id = id(source_payload)
        if source_payload_id in allowed_payload_ids:
            continue
        allowed_payload_ids.add(source_payload_id)
        pending.append(source_payload)


def _candidate_source_payload(
    payloads: tuple[dict[str, object], ...],
    payload: dict[str, object],
) -> dict[str, object] | None:
    source = _mapping(payload.get("source"))
    if source.get("kind") != "candidate":
        return None
    source_order = _candidate_source_order(payload)
    if source_order is None:
        return None
    candidates = [
        candidate
        for candidate in payloads
        if lineage_payload_order(candidate) == source_order
        and _payload_result_matches_candidate_source(candidate, source)
    ]
    if len(candidates) != 1:
        return _seeded_candidate_source_payload(payloads, source, source_order)
    return candidates[0]


def _payload_produces_lineage(payload: dict[str, object]) -> bool:
    return lineage_payload_order(payload) >= (0, 0)


def _seeded_candidate_source_payload(
    payloads: tuple[dict[str, object], ...],
    source: dict[str, object],
    source_order: tuple[int, int],
) -> dict[str, object] | None:
    audit_round_num, round_num = source_order
    if audit_round_num <= 1 or round_num != 1:
        return None
    candidates = [
        payload
        for payload in payloads
        if (0, 0) <= lineage_payload_order(payload) < source_order
        and _payload_result_matches_candidate_source(payload, source)
    ]
    if not candidates:
        return None
    candidates.sort(key=lineage_payload_order)
    return candidates[-1]


def _candidate_source_order(payload: dict[str, object]) -> tuple[int, int] | None:
    round_num = int_field(payload, "round_num")
    if round_num is None:
        return None
    audit_round_num = nullable_int_field(payload, "audit_round_num")
    if not audit_round_num.valid:
        return None
    if payload.get("role") == "reviewer":
        return (audit_round_num.value or 0, round_num)
    if payload.get("role") == "executor" and round_num > 1:
        return (audit_round_num.value or 0, round_num - 1)
    return None


def _payload_result_matches_candidate_source(
    payload: dict[str, object],
    source: dict[str, object],
) -> bool:
    result = _mapping(payload.get("result"))
    return (
        source.get("node_id") == payload.get("node_id")
        and source.get("commit") == result.get("result_commit")
        and source.get("tree") == result.get("result_tree")
        and source.get("candidate_sequence") == 1
        and _payload_bundle_matches_candidate_source(payload, source)
    )


def _payload_bundle_matches_candidate_source(
    payload: dict[str, object],
    source: dict[str, object],
) -> bool:
    bundle = _mapping(payload.get("bundle"))
    refs = _mapping(payload.get("refs"))
    return (
        isinstance(source.get("bundle_sha256"), str)
        and source.get("bundle_path") == bundle.get("path")
        and source.get("bundle_sha256") == bundle.get("sha256")
        and source.get("bundle_size_bytes") == bundle.get("size_bytes")
        and source.get("bundle_ref") == refs.get("result")
    )


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}
