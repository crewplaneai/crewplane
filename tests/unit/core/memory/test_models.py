from __future__ import annotations

import pytest
from pydantic import ValidationError

from crewplane.core.memory.candidates import CandidateDocument, LessonProposal
from crewplane.core.memory.lifecycle import Decision, accept_batch, import_candidates
from crewplane.core.memory.models import (
    EvidenceReceipt,
    MemoryRecord,
    MemoryRevision,
    MemorySnapshot,
    ProposalProvenance,
)
from crewplane.core.memory.query import QueryContribution, build_query
from crewplane.core.memory.retrieval import select_memory
from crewplane.version import SCHEMA_VERSION
from tests.helpers.memory import accepted_snapshot, evidence_receipt, lesson_document


def test_observation_capture_and_acceptance_dates_do_not_change_recall_identity():
    evidence = evidence_receipt()
    document = lesson_document(
        LessonProposal(
            title="Test reporting",
            body="Verify reported tests.",
            tags=("tests",),
            qualifications=("Agent reports are claims.",),
            evidence_ids=(evidence.evidence_id,),
        )
    )
    snapshot, ids = import_candidates(
        MemorySnapshot(),
        document,
        ProposalProvenance(
            author="extractor", source_identity="proposal", run_id="run"
        ),
        {evidence.evidence_id: evidence},
    )
    snapshot = accept_batch(
        snapshot,
        (
            Decision(
                ids[0].record_id,
                ids[0].revision_id,
                ids[0].candidate_digest,
                None,
                "accept",
            ),
        ),
    )
    payload = snapshot.model_dump(mode="json")
    revision = payload["records"][0]["revisions"][0]
    revision["created_at"] = "later"
    revision["acceptance"]["accepted_at"] = "later"
    revision["evidence"][0]["observed_at"] = "later"
    revision["evidence"][0]["captured_at"] = "later"
    changed = MemorySnapshot.model_validate(payload)
    query = build_query(
        (QueryContribution(kind="literal", source="task", text="tests"),)
    )
    first = select_memory(snapshot, "node", "executor", query)
    second = select_memory(changed, "node", "executor", query)
    assert first.semantic_payload() == second.semantic_payload()
    assert "Agent reports are claims." in first.rendered
    assert "agent_output" in first.rendered
    assert "not captured test evidence" in first.rendered


@pytest.mark.parametrize(
    "field", ["content_digest", "evidence_digest", "candidate_digest"]
)
def test_revision_content_and_evidence_digests_are_checked(field):
    snapshot = accepted_snapshot(LessonProposal(title="Files", body="Protect files."))
    payload = snapshot.records[0].revisions[0].model_dump(mode="json")
    payload[field] = "0" * 64
    with pytest.raises(ValidationError):
        MemoryRevision.model_validate(payload)


def test_current_shapes_reject_invalid_lifecycle_and_forged_evidence():
    snapshot = accepted_snapshot(LessonProposal(title="Files", body="Protect files."))
    record = snapshot.records[0]
    revision = record.revisions[0]
    for update in (
        {"accepted_revision": "missing"},
        {"revisions": [revision, revision]},
        {"accepted_revision": None},
    ):
        with pytest.raises(ValidationError):
            MemoryRecord.model_validate({**record.model_dump(), **update})
    for update in (
        {"acceptance": None},
        {"expected_accepted_revision": "missing"},
        {"disposition": "pending"},
    ):
        with pytest.raises(ValidationError):
            MemoryRevision.model_validate({**revision.model_dump(), **update})
    with pytest.raises(ValidationError):
        MemorySnapshot(records=(record, record))
    with pytest.raises(ValidationError):
        MemorySnapshot(schema_version="future")
    with pytest.raises(ValidationError):
        EvidenceReceipt.model_validate(
            {**evidence_receipt().model_dump(), "excerpt": "tampered"}
        )
    with pytest.raises(ValidationError):
        revision.body = "mutable"


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": "future", "lessons": []},
        {"schema_version": "2", "lessons": []},
        {
            "schema_version": SCHEMA_VERSION,
            "lessons": [{"title": "X", "body": "Y", "corrects_record": "m-1"}],
        },
    ],
)
def test_unsupported_and_malformed_candidate_documents(payload):
    with pytest.raises(ValidationError):
        CandidateDocument.model_validate(payload)


def test_invalid_evidence_and_stale_batch_do_not_mutate_input_snapshot():
    snapshot = MemorySnapshot()
    proposal = LessonProposal(
        title="Claim", body="Some claim.", evidence_ids=("invented",)
    )
    with pytest.raises(ValueError, match="Unknown host"):
        import_candidates(
            snapshot,
            lesson_document(proposal),
            ProposalProvenance(author="human", source_identity="manual"),
            {},
        )
    assert not snapshot.records
    snapshot, ids = import_candidates(
        snapshot,
        lesson_document(
            LessonProposal(title="First", body="First lesson."),
            LessonProposal(title="Second", body="Second lesson."),
        ),
        ProposalProvenance(author="human", source_identity="manual"),
        {},
    )
    first, second = ids
    decisions = (
        Decision(
            first.record_id, first.revision_id, first.candidate_digest, None, "accept"
        ),
        Decision(second.record_id, second.revision_id, "0" * 64, None, "reject"),
    )
    with pytest.raises(ValueError, match="Stale"):
        accept_batch(snapshot, decisions)
    assert all(record.accepted_revision is None for record in snapshot.records)
    with pytest.raises(ValueError, match="Duplicate"):
        accept_batch(snapshot, (decisions[0], decisions[0]))
