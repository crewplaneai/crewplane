from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from .candidates import CandidateDocument, LessonProposal
from .models import (
    Acceptance,
    EvidenceReceipt,
    MemoryRecord,
    MemoryRevision,
    MemorySnapshot,
    ProposalProvenance,
    digest_payload,
)


@dataclass(frozen=True)
class CandidateIdentity:
    record_id: str
    revision_id: str
    candidate_digest: str
    expected_accepted_revision: str | None


@dataclass(frozen=True)
class Decision:
    record_id: str
    revision_id: str
    candidate_digest: str
    expected_accepted_revision: str | None
    action: Literal["accept", "reject"]


def import_candidates(
    snapshot: MemorySnapshot,
    document: CandidateDocument,
    provenance: ProposalProvenance,
    evidence: dict[str, EvidenceReceipt],
) -> tuple[MemorySnapshot, tuple[CandidateIdentity, ...]]:
    records = list(snapshot.records)
    imported: list[CandidateIdentity] = []
    for proposal in document.lessons:
        missing = set(proposal.evidence_ids) - evidence.keys()
        if missing:
            raise ValueError(f"Unknown host evidence IDs: {', '.join(sorted(missing))}")
        receipts = tuple(evidence[key] for key in proposal.evidence_ids)
        identity = digest_payload(
            {
                "proposal": proposal.model_dump(mode="json"),
                "provenance": provenance.model_dump(mode="json"),
                "evidence": [item.semantic_payload() for item in receipts],
            }
        )
        existing = next(
            (
                (record, revision)
                for record in records
                for revision in record.revisions
                if revision.proposal_identity == identity
            ),
            None,
        )
        if existing is not None:
            imported.append(candidate_identity(*existing))
            continue
        revision = new_revision(proposal, provenance, receipts, identity)
        if proposal.corrects_record is None:
            record = MemoryRecord(record_id=f"m-{uuid4().hex}", revisions=(revision,))
            records.append(record)
        else:
            record = find_record(records, proposal.corrects_record)
            if record.accepted_revision != proposal.expected_accepted_revision:
                raise ValueError(
                    "Stale correction: inspect the current accepted revision."
                )
            updated = MemoryRecord(
                record_id=record.record_id,
                revisions=(*record.revisions, revision),
                accepted_revision=record.accepted_revision,
            )
            records[records.index(record)] = updated
            record = updated
        imported.append(candidate_identity(record, revision))
    if tuple(records) == snapshot.records:
        return snapshot, tuple(imported)
    return changed_snapshot(snapshot, tuple(records)), tuple(imported)


def new_revision(
    proposal: LessonProposal,
    provenance: ProposalProvenance,
    evidence: tuple[EvidenceReceipt, ...],
    proposal_identity: str,
) -> MemoryRevision:
    content = {
        "title": proposal.title,
        "body": proposal.body,
        "tags": list(proposal.tags),
        "qualifications": list(proposal.qualifications),
    }
    identities = {
        "content_digest": digest_payload(content),
        "evidence_digest": digest_payload(
            [item.semantic_payload() for item in evidence]
        ),
        "provenance": provenance.model_dump(mode="json"),
        "expected_accepted_revision": proposal.expected_accepted_revision,
    }
    return MemoryRevision(
        revision_id=f"r-{uuid4().hex}",
        title=proposal.title,
        body=proposal.body,
        tags=proposal.tags,
        qualifications=proposal.qualifications,
        evidence=evidence,
        provenance=provenance,
        proposal_identity=proposal_identity,
        expected_accepted_revision=proposal.expected_accepted_revision,
        content_digest=str(identities["content_digest"]),
        evidence_digest=str(identities["evidence_digest"]),
        candidate_digest=digest_payload(identities),
        created_at=datetime.now(UTC).isoformat(),
    )


def candidate_identity(
    record: MemoryRecord, revision: MemoryRevision
) -> CandidateIdentity:
    return CandidateIdentity(
        record.record_id,
        revision.revision_id,
        revision.candidate_digest,
        revision.expected_accepted_revision,
    )


def find_record(
    records: list[MemoryRecord] | tuple[MemoryRecord, ...], record_id: str
) -> MemoryRecord:
    record = next((item for item in records if item.record_id == record_id), None)
    if record is None:
        raise ValueError(f"Stale or missing memory record {record_id!r}.")
    return record


def checked_candidate(
    record: MemoryRecord, decision: Decision, require_candidate_predecessor: bool = True
) -> MemoryRevision:
    revision = next(
        (item for item in record.revisions if item.revision_id == decision.revision_id),
        None,
    )
    if (
        revision is None
        or revision.disposition != "pending"
        or revision.candidate_digest != decision.candidate_digest
        or record.accepted_revision != decision.expected_accepted_revision
        or require_candidate_predecessor
        and revision.expected_accepted_revision != decision.expected_accepted_revision
    ):
        raise ValueError(
            f"Stale memory decision for {record.record_id}; inspect its current revision before retrying."
        )
    return revision


def accept_batch(
    snapshot: MemorySnapshot, decisions: tuple[Decision, ...]
) -> MemorySnapshot:
    if len({(d.record_id, d.revision_id) for d in decisions}) != len(decisions):
        raise ValueError("Duplicate memory decisions are not permitted.")
    accepts = [
        decision.record_id for decision in decisions if decision.action == "accept"
    ]
    if len(set(accepts)) != len(accepts):
        raise ValueError(
            "Multiple accepted revisions for one memory record are not permitted."
        )
    checked: list[MemoryRevision] = []
    for decision in decisions:
        if decision.action not in {"accept", "reject"}:
            raise ValueError("Unknown memory decision action.")
        checked.append(
            checked_candidate(
                find_record(snapshot.records, decision.record_id),
                decision,
                decision.action == "accept",
            )
        )
    records = list(snapshot.records)
    for decision, revision in zip(decisions, checked, strict=True):
        record = find_record(records, decision.record_id)
        updates: dict[str, object] = {"disposition": "rejected"}
        accepted_id = record.accepted_revision
        if decision.action == "accept":
            updates = {
                "disposition": "accepted",
                "acceptance": Acceptance(
                    accepted_at=datetime.now(UTC).isoformat(),
                    candidate_digest=revision.candidate_digest,
                    expected_accepted_revision=decision.expected_accepted_revision,
                ),
            }
            accepted_id = revision.revision_id
        revisions = tuple(
            revise(item, updates)
            if item.revision_id == revision.revision_id
            else revise(item, {"disposition": "superseded"})
            if decision.action == "accept"
            and item.revision_id == record.accepted_revision
            else item
            for item in record.revisions
        )
        records[records.index(record)] = MemoryRecord(
            record_id=record.record_id,
            revisions=revisions,
            accepted_revision=accepted_id,
        )
    return changed_snapshot(snapshot, tuple(records)) if decisions else snapshot


def edit_candidate(
    snapshot: MemorySnapshot, decision: Decision, proposal: LessonProposal
) -> MemorySnapshot:
    record = find_record(snapshot.records, decision.record_id)
    old = checked_candidate(record, decision, False)
    if (proposal.corrects_record, proposal.expected_accepted_revision) != (
        record.record_id if record.accepted_revision else None,
        record.accepted_revision,
    ):
        raise ValueError(
            "Editing must target the currently inspected accepted revision."
        )
    if proposal.evidence_ids != tuple(item.evidence_id for item in old.evidence):
        raise ValueError("Editing must preserve host evidence references.")
    provenance = ProposalProvenance(
        author="human",
        source_identity=f"edit:{record.record_id}:{old.revision_id}",
        run_id=old.provenance.run_id,
        extraction_run_id=old.provenance.extraction_run_id,
    )
    revision = new_revision(
        proposal,
        provenance,
        old.evidence,
        digest_payload(
            {"edit": old.revision_id, "proposal": proposal.model_dump(mode="json")}
        ),
    )
    updated = MemoryRecord(
        record_id=record.record_id,
        accepted_revision=record.accepted_revision,
        revisions=(
            *(
                revise(item, {"disposition": "superseded"}) if item == old else item
                for item in record.revisions
            ),
            revision,
        ),
    )
    return changed_snapshot(
        snapshot,
        tuple(updated if item == record else item for item in snapshot.records),
    )


def forget_record(
    snapshot: MemorySnapshot, record_id: str, expected_state_digest: str
) -> MemorySnapshot:
    record = find_record(snapshot.records, record_id)
    if record.state_digest != expected_state_digest:
        raise ValueError(
            "Stale forget request: inspect the complete record before deleting it."
        )
    return changed_snapshot(
        snapshot, tuple(item for item in snapshot.records if item != record)
    )


def revise(revision: MemoryRevision, updates: dict[str, object]) -> MemoryRevision:
    return MemoryRevision.model_validate({**revision.model_dump(), **updates})


def changed_snapshot(
    snapshot: MemorySnapshot, records: tuple[MemoryRecord, ...]
) -> MemorySnapshot:
    return MemorySnapshot(
        schema_version=snapshot.schema_version,
        transaction_id=uuid4().hex,
        records=records,
    )
