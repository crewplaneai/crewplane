from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from crewplane.core.memory.candidates import parse_candidates
from crewplane.core.memory.lifecycle import CandidateIdentity, import_candidates
from crewplane.core.memory.models import (
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
    document = parse_candidates(markdown)
    evidence = {
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
    for proposal in document.lessons:
        if not proposal.evidence_ids:
            raise ValueError(
                "Extracted lessons must cite at least one host evidence ID; no useful lesson may instead return an empty lessons list."
            )
        if set(proposal.evidence_ids) - evidence.keys():
            raise ValueError("Extractor cited unknown host evidence IDs.")
    proposal_identity = digest_payload(
        {
            "packet": packet.packet_digest,
            "document": document.model_dump(mode="json"),
            "extraction_run_id": extraction_run_id,
        }
    )
    provenance = ProposalProvenance(
        author="extractor",
        source_identity=proposal_identity,
        run_id=packet.parent_run_id,
        extraction_run_id=extraction_run_id,
    )
    validated = {
        "packet_digest": packet.packet_digest,
        "proposal_identity": proposal_identity,
        "document": document.model_dump(mode="json"),
        "provenance": provenance.model_dump(mode="json"),
        "imported": None,
    }
    publish_receipt(parent_stages, "candidates.json", validated)

    def mutate(
        snapshot: MemorySnapshot,
    ) -> tuple[MemorySnapshot, tuple[CandidateIdentity, ...]]:
        return import_candidates(snapshot, document, provenance, evidence)

    imported = await MemoryStore(project_root).transact(mutate)
    publish_receipt(
        parent_stages,
        "candidates.json",
        {**validated, "imported": [asdict(identity) for identity in imported]},
    )
    return imported
