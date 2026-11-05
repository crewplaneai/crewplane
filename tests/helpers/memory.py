from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from crewplane.artifacts.memory.store import MemoryStore
from crewplane.core.memory.candidates import CandidateDocument, LessonProposal
from crewplane.core.memory.lifecycle import Decision, accept_batch, import_candidates
from crewplane.core.memory.models import (
    EvidenceReceipt,
    MemorySnapshot,
    ProposalProvenance,
)
from crewplane.version import SCHEMA_VERSION


def lesson_document(*lessons: LessonProposal) -> CandidateDocument:
    return CandidateDocument(schema_version=SCHEMA_VERSION, lessons=lessons)


def accepted_snapshot(*lessons: LessonProposal) -> MemorySnapshot:
    snapshot, identities = import_candidates(
        MemorySnapshot(),
        lesson_document(*lessons),
        ProposalProvenance(author="human", source_identity="test-author"),
        {},
    )
    return accept_batch(
        snapshot,
        tuple(
            Decision(
                item.record_id,
                item.revision_id,
                item.candidate_digest,
                item.expected_accepted_revision,
                "accept",
            )
            for item in identities
        ),
    )


def initialize_memory(
    root: Path, snapshot: MemorySnapshot | None = None
) -> MemorySnapshot:
    async def initialize() -> MemorySnapshot:
        store = MemoryStore(root)
        initial = await store.initialize()
        if snapshot is None:
            return initial

        def replace(_current: MemorySnapshot) -> tuple[MemorySnapshot, MemorySnapshot]:
            del _current
            return snapshot, snapshot

        return await store.transact(replace)

    return asyncio.run(initialize())


def evidence_receipt(
    text: str = "An agent said tests passed.",
    run_id: str = "run",
    node_id: str = "node",
) -> EvidenceReceipt:
    digest = hashlib.sha256(text.encode()).hexdigest()
    return EvidenceReceipt(
        evidence_id="e-source",
        kind="agent_output",
        source_locator=".crewplane/execution-results/run/node-result.md",
        source_sha256=digest,
        excerpt=text,
        excerpt_sha256=digest,
        observed_at="2026-01-01T00:00:00Z",
        captured_at="2026-01-02T00:00:00Z",
        run_id=run_id,
        node_id=node_id,
        qualifications=("Agent-reported, not captured test evidence.",),
    )
