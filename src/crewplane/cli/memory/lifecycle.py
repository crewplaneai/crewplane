from __future__ import annotations

from pathlib import Path

from crewplane.artifacts.memory.store import MemoryStore
from crewplane.core.memory.candidates import CandidateDocument
from crewplane.core.memory.lifecycle import (
    CandidateIdentity,
    Decision,
    accept_batch,
    forget_record,
    import_candidates,
)
from crewplane.core.memory.models import (
    MemorySnapshot,
    ProposalProvenance,
    digest_payload,
)


async def propose_document(
    project_root: Path, document: CandidateDocument
) -> tuple[CandidateIdentity, ...]:
    if any(lesson.evidence_ids for lesson in document.lessons):
        raise ValueError(
            "Manual proposals cannot supply host evidence IDs; use retained extraction proposals for execution evidence."
        )
    provenance = ProposalProvenance(
        author="human",
        source_identity="manual:" + digest_payload(document.model_dump(mode="json")),
    )

    def mutate(
        snapshot: MemorySnapshot,
    ) -> tuple[MemorySnapshot, tuple[CandidateIdentity, ...]]:
        return import_candidates(snapshot, document, provenance, {})

    return await MemoryStore(project_root).transact(mutate)


async def decide(project_root: Path, decisions: tuple[Decision, ...]) -> None:
    def mutate(snapshot: MemorySnapshot) -> tuple[MemorySnapshot, None]:
        return accept_batch(snapshot, decisions), None

    await MemoryStore(project_root).transact(mutate)


async def forget(project_root: Path, record_id: str, state_digest: str) -> None:
    def mutate(snapshot: MemorySnapshot) -> tuple[MemorySnapshot, None]:
        return forget_record(snapshot, record_id, state_digest), None

    await MemoryStore(project_root).transact(mutate)
