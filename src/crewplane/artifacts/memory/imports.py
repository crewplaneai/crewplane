"""Prepare and import memory candidates, publishing receipts around persistence."""

from __future__ import annotations

import asyncio
from contextvars import copy_context
from dataclasses import asdict
from functools import partial
from pathlib import Path

from crewplane.core.memory.candidates import CandidateDocument, parse_candidates
from crewplane.core.memory.lifecycle import CandidateIdentity, import_candidates
from crewplane.core.memory.models import (
    EvidenceReceipt,
    MemorySnapshot,
    ProposalProvenance,
    digest_payload,
)
from crewplane.core.memory.packets import EvidencePacket

from .receipts import publish_receipt
from .store import MemoryStore


async def import_extraction(
    project_root: Path,
    parent_stages: Path,
    packet: EvidencePacket,
    markdown: str,
    extraction_run_id: str,
) -> tuple[CandidateIdentity, ...]:
    """Import extractor proposals into explicitly initialized project memory.

    The caller must hold the parent stages' extraction-attempt lock and supply
    its retained host packet and extraction run ID. Return candidate identities
    in document order, including their revision, digest, and expected accepted revision;
    an empty lessons list returns an empty tuple. Import never accepts proposals
    or changes existing dispositions. Retrying the same parsed document, packet,
    and extraction run ID reuses retained proposals.

    Malformed documents or missing/unknown evidence references raise ValueError
    before publication. Store validation can still fail during the transaction.
    A validated candidates.json receipt is published before the store transaction,
    then replaced with the imported identities after commit. Store or receipt
    errors propagate: final receipt publication can fail after candidates commit,
    so failure does not imply rollback. Inspect uncertain store publication before
    retrying. Cancellation waits for an active receipt write before propagating;
    any write error is retained as its cause. The caller keeps its lock until I/O ends.
    """
    document = parse_candidates(markdown)
    evidence = _prepare_host_evidence(packet)
    _validate_evidence_references(document, evidence)
    provenance = _prepare_provenance(document, packet, extraction_run_id)
    validated = {
        "packet_digest": packet.packet_digest,
        "proposal_identity": provenance.source_identity,
        "document": document.model_dump(mode="json"),
        "provenance": provenance.model_dump(mode="json"),
        "imported": None,
    }
    await _publish_candidates_receipt(parent_stages, validated)

    def mutate(
        snapshot: MemorySnapshot,
    ) -> tuple[MemorySnapshot, tuple[CandidateIdentity, ...]]:
        return import_candidates(snapshot, document, provenance, evidence)

    imported = await MemoryStore(project_root).transact(mutate)
    await _publish_candidates_receipt(
        parent_stages,
        {**validated, "imported": [asdict(identity) for identity in imported]},
    )
    return imported


def _prepare_host_evidence(packet: EvidencePacket) -> dict[str, EvidenceReceipt]:
    return {
        item.evidence_id: item.model_copy(
            update={
                "qualifications": tuple(
                    dict.fromkeys((*packet.qualifications, *item.qualifications))
                ),
                "provenance": (
                    *item.provenance,
                    f"Host packet {packet.packet_digest}; parent {packet.parent_run_key}; parent outcome: {packet.parent_outcome}.",
                ),
            }
        )
        for item in packet.evidence
    }


def _validate_evidence_references(
    document: CandidateDocument, evidence: dict[str, EvidenceReceipt]
) -> None:
    for proposal in document.lessons:
        if not proposal.evidence_ids:
            raise ValueError(
                "Extracted lessons must cite at least one host evidence ID; no useful lesson may instead return an empty lessons list."
            )
        if set(proposal.evidence_ids) - evidence.keys():
            raise ValueError("Extractor cited unknown host evidence IDs.")


def _prepare_provenance(
    document: CandidateDocument, packet: EvidencePacket, extraction_run_id: str
) -> ProposalProvenance:
    proposal_identity = digest_payload(
        {
            "packet": packet.packet_digest,
            "document": document.model_dump(mode="json"),
            "extraction_run_id": extraction_run_id,
        }
    )
    return ProposalProvenance(
        author="extractor",
        source_identity=proposal_identity,
        run_id=packet.parent_run_id,
        extraction_run_id=extraction_run_id,
    )


async def _publish_candidates_receipt(parent_stages: Path, payload: object) -> None:
    """Finish receipt I/O before cancellation can release the extraction lock."""
    publish = partial(publish_receipt, parent_stages, "candidates.json", payload)
    worker = asyncio.get_running_loop().run_in_executor(
        None, copy_context().run, publish
    )
    cancelled: asyncio.CancelledError | None = None
    while not worker.done():
        try:
            await asyncio.wait((worker,))
        except asyncio.CancelledError as exc:
            cancelled = exc
    try:
        worker.result()
    except Exception as exc:
        if cancelled is not None:
            raise cancelled from exc
        raise
    if cancelled is not None:
        raise cancelled
