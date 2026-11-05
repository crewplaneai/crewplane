from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from crewplane.architecture.contracts import NodeArtifactRequest
from crewplane.architecture.safe_file_reads import read_contained_bytes
from crewplane.artifacts.atomic import json_bytes
from crewplane.artifacts.expansions import reconstruct_expansions
from crewplane.artifacts.naming import node_state_relative_path
from crewplane.artifacts.verification import read_verified_node_artifact
from crewplane.core.execution_state import NodeState, RunManifest
from crewplane.core.memory.models import EvidenceReceipt, digest_payload
from crewplane.core.memory.packets import EvidencePacket
from crewplane.core.preflight.models import PreflightExecutionPlan

from .outcomes import (
    STRUCTURED_LIMIT_BYTES,
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
    try:
        saved = read_packet(stages_dir)
    except FileNotFoundError:
        saved = None
    if saved is not None:
        if (
            saved.parent_run_id != plan.run_id
            or saved.workflow_signature != plan.workflow_signature
            or saved.parent_run_key != plan.run_key_name
        ):
            raise ValueError(
                "Saved memory packet does not belong to the frozen parent plan."
            )
        return saved
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
            lineage = [f"Parent workflow signature: {plan.workflow_signature}"]
            if invocations.source_sha256 is not None:
                lineage.append(
                    f"Framework event source: {stages_dir / 'logs/events.ndjson'}; SHA256 {invocations.source_sha256}"
                )
                lineage.extend(
                    f"Current invocation: {event['task_id']}; provider {event['provider']}; role {event['role']}; audit {event.get('audit_round_num')}; round {event['round_num']}; {event['event_type']}."
                    for event in invocations.events
                    if event.get("node_id") == node.id
                    and event.get("event_type") != "invocation_started"
                )
            if state.resume_origin is not None:
                lineage.append(
                    "Includes resumed work: " + state.resume_origin.model_dump_json()
                )
            for checkpoint in manifest.resumed_review_checkpoints:
                if checkpoint.node_id == node.id:
                    lineage.append(
                        "Checkpoint reuse plus new invocations: "
                        + checkpoint.model_dump_json()
                    )
            if state.workspace is not None:
                lineage.append(
                    "Retained workspace provenance: "
                    + json.dumps(state.workspace, sort_keys=True)
                )
            for kind in ("output", "findings") if node.findings else ("output",):
                verified = read_verified_node_artifact(
                    stages_dir,
                    results_dir,
                    NodeArtifactRequest(node.id, node.artifact_contract),
                    kind,
                    SOURCE_BYTE_LIMIT,
                    STRUCTURED_LIMIT_BYTES,
                )
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
                evidence.append(
                    EvidenceReceipt(
                        evidence_id="e-"
                        + digest_payload(
                            {"node": node.id, "kind": kind, "source": verified.sha256}
                        )[:24],
                        kind="agent_output",
                        source_locator=verified.path.as_posix(),
                        source_sha256=verified.sha256,
                        excerpt=excerpt,
                        excerpt_sha256=hashlib.sha256(
                            excerpt.encode("utf-8")
                        ).hexdigest(),
                        observed_at=state.completed_at,
                        captured_at=captured_at,
                        run_id=plan.run_id,
                        node_id=node.id,
                        qualifications=excerpt_limits,
                        provenance=tuple(lineage),
                    )
                )
        except (OSError, ValueError, UnicodeError) as exc:
            omissions.append(
                f"{node.id}: retained evidence unavailable, invalid, or oversized ({type(exc).__name__}: {exc})."
            )
    if not evidence:
        raise NoEligibleEvidence(
            "No new usable retained evidence from memory-enabled nodes. "
            + " ".join(omissions)
        )
    packet = pack_evidence(
        plan,
        manifest.status,
        captured_at,
        tuple(qualifications),
        tuple(evidence),
        tuple(omissions),
    )
    if not packet.evidence:
        raise NoEligibleEvidence(
            "No complete evidence unit fits the 64 KiB memory packet; reduce retained lesson source size or outcome context."
        )
    save_packet(stages_dir, packet)
    return packet


def pack_evidence(
    plan: PreflightExecutionPlan,
    outcome: str,
    captured_at: str,
    qualifications: tuple[str, ...],
    evidence: tuple[EvidenceReceipt, ...],
    omissions: tuple[str, ...],
) -> EvidencePacket:
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
