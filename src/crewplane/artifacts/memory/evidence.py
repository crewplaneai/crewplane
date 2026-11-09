"""Capture verified retained evidence, pack bounded packets, and persist them."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from crewplane.architecture.contracts import NodeArtifactRequest, VerifiedNodeArtifact
from crewplane.architecture.safe_file_reads import read_contained_bytes
from crewplane.artifacts.atomic import json_bytes
from crewplane.artifacts.expansions import reconstruct_expansions
from crewplane.artifacts.naming import node_state_relative_path
from crewplane.artifacts.verification import read_verified_node_artifact
from crewplane.core.execution_state import NodeState, RunManifest
from crewplane.core.memory.models import EvidenceReceipt, digest_payload
from crewplane.core.memory.packets import EvidencePacket
from crewplane.core.preflight.compiled_records import PreflightExecutionNode
from crewplane.core.preflight.models import PreflightExecutionPlan

from .outcomes import (
    STRUCTURED_LIMIT_BYTES,
    InvocationEvidence,
    has_new_execution,
    node_invocation_qualifiers,
    read_invocations,
    read_process_evidence,
    review_qualifiers,
)
from .receipts import PACKET_LIMIT_BYTES, read_packet, save_packet

EXCERPT_CHAR_LIMIT = 4096
SOURCE_BYTE_LIMIT = 16 * 1024 * 1024


class NoEligibleEvidence(ValueError):
    """No new enabled, verified retained source can support extraction."""


def capture_evidence(
    plan: PreflightExecutionPlan, stages_dir: Path, results_dir: Path
) -> EvidencePacket:
    """Reuse a saved packet or capture and persist evidence for the frozen plan.

    A valid saved packet matching the plan's run ID, run key, and workflow
    signature is returned without rechecking retained sources. If no packet
    exists, capture requires a matching succeeded or failed run manifest and
    collects verified artifacts from memory-enabled nodes with new execution.
    Receipts follow expanded-plan order, with output before optional findings;
    source excerpts contain at most the first 4096 characters.

    Per-node I/O, validation, and decoding failures become omissions. Receipts
    already collected from that node remain eligible. Invocation failures and
    unavailable lifecycle, review, or process evidence remain explicit in the
    qualifications rather than being treated as proof of success. Run-level
    validation and I/O failures propagate.

    A new packet retains mandatory outcome context, fits within 64 KiB, and
    contains at least one complete receipt. It is atomically saved to
    ``stages_dir/manifests/memory/evidence.json`` before being returned.

    Args:
        plan: Frozen parent execution plan identifying the run and enabled nodes.
        stages_dir: Parent run's retained execution-stage directory.
        results_dir: Parent run's retained execution-results directory.

    Returns:
        The matching saved packet or the newly persisted evidence packet.

    Raises:
        NoEligibleEvidence: The parent is ineligible, no usable new evidence
            remains, no complete receipt fits, or mandatory context exceeds
            the packet bound.
        ValueError: Saved-packet identity or other run-level validation fails.
        OSError: A run-level read or packet persistence operation fails.
    """
    saved = _read_saved_packet(plan, stages_dir)
    if saved is not None:
        return saved
    manifest = _read_completed_manifest(plan, stages_dir)
    collected = _collect_node_evidence(plan, manifest, stages_dir, results_dir)
    if not collected.evidence:
        raise NoEligibleEvidence(
            "No new usable retained evidence from memory-enabled nodes. "
            + " ".join(collected.omissions)
        )
    packet = pack_evidence(
        plan,
        manifest.status,
        collected.captured_at,
        collected.qualifications,
        collected.evidence,
        collected.omissions,
    )
    if not packet.evidence:
        raise NoEligibleEvidence(
            "No complete evidence unit fits the 64 KiB memory packet; reduce retained lesson source size or outcome context."
        )
    save_packet(stages_dir, packet)
    return packet


def _read_saved_packet(
    plan: PreflightExecutionPlan, stages_dir: Path
) -> EvidencePacket | None:
    try:
        saved = read_packet(stages_dir)
    except FileNotFoundError:
        return None
    if (
        saved.parent_run_id != plan.run_id
        or saved.workflow_signature != plan.workflow_signature
        or saved.parent_run_key != plan.run_key_name
    ):
        raise ValueError(
            "Saved memory packet does not belong to the frozen parent plan."
        )
    return saved


def _read_completed_manifest(
    plan: PreflightExecutionPlan, stages_dir: Path
) -> RunManifest:
    manifest = RunManifest.model_validate_json(
        read_contained_bytes(stages_dir, "manifests/run.json", SOURCE_BYTE_LIMIT)
    )
    if (
        manifest.status not in {"succeeded", "failed"}
        or manifest.run_id != plan.run_id
        or manifest.workflow_signature != plan.workflow_signature
    ):
        raise NoEligibleEvidence(
            "Memory extraction requires a matching completed, non-cancelled parent run."
        )
    return manifest


@dataclass(frozen=True)
class _CollectedNodeEvidence:
    captured_at: str
    qualifications: tuple[str, ...]
    evidence: tuple[EvidenceReceipt, ...]
    omissions: tuple[str, ...]


def _collect_node_evidence(
    plan: PreflightExecutionPlan,
    manifest: RunManifest,
    stages_dir: Path,
    results_dir: Path,
) -> _CollectedNodeEvidence:
    expanded = reconstruct_expansions(plan, stages_dir)
    invocations = read_invocations(stages_dir, plan.run_id)
    processes = read_process_evidence(stages_dir, plan.run_id)
    captured_at = datetime.now(UTC).isoformat()
    qualifications = [
        f"Parent workflow outcome: {manifest.status}. Success does not prove a lesson correct or tests passed.",
        "Agent output reports claims. Framework lifecycle evidence reports invocation outcomes. Provider receipts report process exits. Human acceptance does not upgrade these evidence categories.",
    ]
    omissions = [
        f"Generated group {group}: frozen expansion is unavailable or invalid."
        for group in sorted(expanded.invalid_groups)
    ]
    evidence: list[EvidenceReceipt] = []
    for node in expanded.plan.nodes:
        if not node.memory:
            continue
        try:
            state = _read_matching_node_state(plan, node, stages_dir)
            if not has_new_execution(node, state, manifest, invocations):
                omissions.append(
                    f"{node.id}: entirely reused retained work; no new invocation evidence."
                )
                continue
            limits = (
                *node_invocation_qualifiers(node.id, invocations),
                *review_qualifiers(stages_dir, node),
                *processes.qualifications(node.id),
            )
            qualifications.extend(f"{node.id}: {value}" for value in limits)
            lineage = _node_provenance(state, manifest, invocations, stages_dir)
            for kind in ("output", "findings") if node.findings else ("output",):
                verified = read_verified_node_artifact(
                    stages_dir,
                    results_dir,
                    NodeArtifactRequest(node.id, node.artifact_contract),
                    kind,
                    SOURCE_BYTE_LIMIT,
                    STRUCTURED_LIMIT_BYTES,
                )
                evidence.append(
                    _receipt_for_artifact(
                        verified, kind, state, captured_at, limits, lineage
                    )
                )
        except (OSError, ValueError, UnicodeError) as exc:
            omissions.append(
                f"{node.id}: retained evidence unavailable, invalid, or oversized ({type(exc).__name__}: {exc})."
            )
    return _CollectedNodeEvidence(
        captured_at, tuple(qualifications), tuple(evidence), tuple(omissions)
    )


def _read_matching_node_state(
    plan: PreflightExecutionPlan, node: PreflightExecutionNode, stages_dir: Path
) -> NodeState:
    state = NodeState.model_validate_json(
        read_contained_bytes(
            stages_dir,
            node_state_relative_path(node.id).as_posix(),
            STRUCTURED_LIMIT_BYTES,
        )
    )
    if (
        state.run_id != plan.run_id
        or state.workflow_signature != plan.workflow_signature
        or state.node_id != node.id
        or state.expansion != node.expansion
    ):
        raise ValueError("node state identity mismatch")
    return state


def _node_provenance(
    state: NodeState,
    manifest: RunManifest,
    invocations: InvocationEvidence,
    stages_dir: Path,
) -> tuple[str, ...]:
    lineage = [f"Parent workflow signature: {state.workflow_signature}"]
    if invocations.source_sha256 is not None:
        lineage.append(
            f"Framework event source: {stages_dir / 'logs/events.ndjson'}; SHA256 {invocations.source_sha256}"
        )
        lineage.extend(
            f"Current invocation: {event['task_id']}; provider {event['provider']}; role {event['role']}; audit {event.get('audit_round_num')}; round {event['round_num']}; {event['event_type']}."
            for event in invocations.events
            if event.get("node_id") == state.node_id
            and event.get("event_type") != "invocation_started"
        )
    if state.resume_origin is not None:
        lineage.append(
            "Includes resumed work: " + state.resume_origin.model_dump_json()
        )
    for checkpoint in manifest.resumed_review_checkpoints:
        if checkpoint.node_id == state.node_id:
            lineage.append(
                "Checkpoint reuse plus new invocations: " + checkpoint.model_dump_json()
            )
    if state.workspace is not None:
        lineage.append(
            "Retained workspace provenance: "
            + json.dumps(state.workspace, sort_keys=True)
        )
    return tuple(lineage)


def _receipt_for_artifact(
    verified: VerifiedNodeArtifact,
    kind: str,
    state: NodeState,
    captured_at: str,
    limits: tuple[str, ...],
    lineage: tuple[str, ...],
) -> EvidenceReceipt:
    text = verified.payload.decode("utf-8")
    excerpt = text[:EXCERPT_CHAR_LIMIT]
    excerpt_limits = (
        *limits,
        "This excerpt is an agent-reported claim, not independent verification.",
    )
    if len(text) > len(excerpt):
        excerpt_limits = (
            *excerpt_limits,
            f"Excerpt includes only the first {EXCERPT_CHAR_LIMIT} characters; original source SHA256 covers the complete retained artifact.",
        )
    return EvidenceReceipt(
        evidence_id="e-"
        + digest_payload(
            {"node": state.node_id, "kind": kind, "source": verified.sha256}
        )[:24],
        kind="agent_output",
        source_locator=verified.path.as_posix(),
        source_sha256=verified.sha256,
        excerpt=excerpt,
        excerpt_sha256=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
        observed_at=state.completed_at,
        captured_at=captured_at,
        run_id=state.run_id,
        node_id=state.node_id,
        qualifications=excerpt_limits,
        provenance=lineage,
    )


def pack_evidence(
    plan: PreflightExecutionPlan,
    outcome: str,
    captured_at: str,
    qualifications: tuple[str, ...],
    evidence: tuple[EvidenceReceipt, ...],
    omissions: tuple[str, ...],
) -> EvidencePacket:
    """Select whole receipts in input order for a packet of at most 64 KiB.

    Preserve all qualifications and supplied omissions. Greedily include each
    receipt if the serialized packet fits; otherwise append an omission and
    continue considering later receipts. Selected receipts are never split or
    truncated, and their relative order is preserved. Added omission text also
    counts toward the final byte bound.

    This function performs no I/O and may return an empty evidence tuple when
    no receipt fits. ``capture_evidence`` rejects that empty result before
    persistence. Invalid packet contents propagate validation errors rather
    than being treated as receipts that do not fit.

    Args:
        plan: Frozen parent plan supplying the packet's run identity.
        outcome: Parent outcome, validated as ``succeeded`` or ``failed``.
        captured_at: Capture timestamp to retain in the packet.
        qualifications: Mandatory outcome and uncertainty context.
        evidence: Receipts to consider in the supplied order.
        omissions: Existing omissions to retain before packing omissions.

    Returns:
        A validated packet with its content digest and selected receipts.

    Raises:
        NoEligibleEvidence: Mandatory context exceeds the byte bound before
            selection, or added omissions make the final packet exceed it.
        ValueError: Packet contents fail validation.
    """
    selected: list[EvidenceReceipt] = []
    skipped = list(omissions)

    def make_packet(items: tuple[EvidenceReceipt, ...]) -> EvidencePacket:
        identity = {
            "parent_run_id": plan.run_id,
            "parent_run_key": plan.run_key_name,
            "workflow_signature": plan.workflow_signature,
            "parent_outcome": outcome,
            "qualifications": qualifications,
            "evidence": [item.semantic_payload() for item in items],
            "omissions": tuple(skipped),
        }
        return EvidencePacket.model_validate(
            {
                **identity,
                "evidence": [item.model_dump(mode="json") for item in items],
                "captured_at": captured_at,
                "packet_digest": digest_payload(identity),
            }
        )

    if len(json_bytes(make_packet(()).model_dump(mode="json"))) > PACKET_LIMIT_BYTES:
        raise NoEligibleEvidence(
            "Mandatory outcome qualifiers exceed the 64 KiB memory packet bound; extraction skipped rather than omitting uncertainty."
        )
    for item in evidence:
        packet = make_packet((*selected, item))
        if len(json_bytes(packet.model_dump(mode="json"))) <= PACKET_LIMIT_BYTES:
            selected.append(item)
        else:
            skipped.append(
                f"{item.evidence_id}: complete evidence unit does not fit packet byte bound."
            )
    packet = make_packet(tuple(selected))
    if len(json_bytes(packet.model_dump(mode="json"))) > PACKET_LIMIT_BYTES:
        raise NoEligibleEvidence(
            "Evidence omissions and mandatory qualifiers exceed 64 KiB; extraction skipped without dropping outcome context."
        )
    return packet
