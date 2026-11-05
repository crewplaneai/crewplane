import pytest
from pydantic import ValidationError

from crewplane.core.memory.candidates import (
    CandidateDocument,
    LessonProposal,
    parse_candidates,
)
from crewplane.core.memory.lifecycle import (
    Decision,
    accept_batch,
    edit_candidate,
    forget_record,
    import_candidates,
)
from crewplane.core.memory.models import MemorySnapshot, ProposalProvenance
from crewplane.version import SCHEMA_VERSION

PROVENANCE = ProposalProvenance(author="human", source_identity="manual")


def document(body: str = "Keep protected file reads.") -> CandidateDocument:
    return CandidateDocument(
        schema_version=SCHEMA_VERSION,
        lessons=(LessonProposal(title="Protected reads", body=body),),
    )


def test_import_is_pending_idempotent_and_reimport_after_forget_is_fresh():
    snapshot, identities = import_candidates(
        MemorySnapshot(), document(), PROVENANCE, {}
    )
    record = snapshot.records[0]
    revision = record.revisions[0]
    assert record.accepted_revision is None
    duplicate, repeated = import_candidates(snapshot, document(), PROVENANCE, {})
    assert duplicate == snapshot
    assert repeated == identities
    rejected = accept_batch(
        snapshot,
        (
            Decision(
                record.record_id,
                revision.revision_id,
                revision.candidate_digest,
                None,
                "reject",
            ),
        ),
    )
    assert import_candidates(rejected, document(), PROVENANCE, {})[1] == identities
    forgotten = forget_record(
        rejected, record.record_id, rejected.records[0].state_digest
    )
    fresh, new_ids = import_candidates(forgotten, document(), PROVENANCE, {})
    assert new_ids != identities
    assert fresh.records[0].accepted_revision is None


def test_accept_correction_is_atomic_and_stale_batch_preserves_prior_state():
    snapshot, _ = import_candidates(MemorySnapshot(), document(), PROVENANCE, {})
    record = snapshot.records[0]
    revision = record.revisions[0]
    decision = Decision(
        record.record_id,
        revision.revision_id,
        revision.candidate_digest,
        None,
        "accept",
    )
    accepted = accept_batch(snapshot, (decision,))
    proposal = LessonProposal(
        title="Protected reads",
        body="Check symlinks too.",
        corrects_record=record.record_id,
        expected_accepted_revision=revision.revision_id,
    )
    pending, _ = import_candidates(
        accepted,
        CandidateDocument(schema_version=SCHEMA_VERSION, lessons=(proposal,)),
        PROVENANCE,
        {},
    )
    assert pending.records[0].accepted_revision == revision.revision_id
    correction = pending.records[0].revisions[-1]
    corrected = accept_batch(
        pending,
        (
            Decision(
                record.record_id,
                correction.revision_id,
                correction.candidate_digest,
                revision.revision_id,
                "accept",
            ),
        ),
    )
    assert corrected.records[0].accepted_revision == correction.revision_id
    assert corrected.records[0].revisions[0].disposition == "superseded"
    with pytest.raises(ValueError, match="stale|Stale"):
        accept_batch(corrected, (decision,))
    assert pending.records[0].accepted_revision == revision.revision_id


def test_edit_is_immutable_and_digest_bound():
    snapshot, _ = import_candidates(MemorySnapshot(), document(), PROVENANCE, {})
    record, revision = snapshot.records[0], snapshot.records[0].revisions[0]
    edited = edit_candidate(
        snapshot,
        Decision(
            record.record_id,
            revision.revision_id,
            revision.candidate_digest,
            None,
            "accept",
        ),
        document("Changed instruction.").lessons[0],
    )
    assert edited.records[0].revisions[0].body == revision.body
    assert edited.records[0].revisions[-1].body == "Changed instruction."
    assert edited.records[0].revisions[-1].candidate_digest != revision.candidate_digest
    with pytest.raises(ValueError, match="stale|Stale"):
        accept_batch(
            edited,
            (
                Decision(
                    record.record_id,
                    revision.revision_id,
                    revision.candidate_digest,
                    None,
                    "accept",
                ),
            ),
        )


def test_candidate_block_is_exact_and_host_fields_are_forbidden():
    payload = document().model_dump_json()
    assert (
        parse_candidates(f"# Results\n```crewplane-memory-candidates\n{payload}\n```\n")
        == document()
    )
    for malformed in (
        payload,
        f"```json\n{payload}\n```",
        f"```crewplane-memory-candidates\n{payload}\n```\n" * 2,
    ):
        with pytest.raises(ValueError):
            parse_candidates(malformed)
    with pytest.raises(ValidationError):
        LessonProposal(title="Forged", body="bad", accepted=True)


def test_stale_correction_can_be_explicitly_rejected_or_edited_not_accepted():
    snapshot, ids = import_candidates(MemorySnapshot(), document(), PROVENANCE, {})
    initial = ids[0]
    snapshot = accept_batch(
        snapshot,
        (
            Decision(
                initial.record_id,
                initial.revision_id,
                initial.candidate_digest,
                None,
                "accept",
            ),
        ),
    )
    proposals = tuple(
        LessonProposal(
            title="Protected reads",
            body=body,
            corrects_record=initial.record_id,
            expected_accepted_revision=initial.revision_id,
        )
        for body in ("Check symlinks.", "Check hardlinks.")
    )
    pending, corrections = import_candidates(
        snapshot,
        CandidateDocument(schema_version=SCHEMA_VERSION, lessons=proposals),
        PROVENANCE,
        {},
    )
    winner, stale = corrections
    updated = accept_batch(
        pending,
        (
            Decision(
                winner.record_id,
                winner.revision_id,
                winner.candidate_digest,
                initial.revision_id,
                "accept",
            ),
        ),
    )
    for expected in (initial.revision_id, winner.revision_id):
        with pytest.raises(ValueError, match="Stale"):
            accept_batch(
                updated,
                (
                    Decision(
                        stale.record_id,
                        stale.revision_id,
                        stale.candidate_digest,
                        expected,
                        "accept",
                    ),
                ),
            )
    reject = Decision(
        stale.record_id,
        stale.revision_id,
        stale.candidate_digest,
        winner.revision_id,
        "reject",
    )
    rejected = accept_batch(updated, (reject,))
    assert rejected.records[0].revisions[-1].disposition == "rejected"
    assert rejected.records[0].accepted_revision == winner.revision_id
    edited = edit_candidate(
        updated,
        reject,
        LessonProposal(
            title="Protected reads",
            body="Check both links.",
            corrects_record=initial.record_id,
            expected_accepted_revision=winner.revision_id,
        ),
    )
    assert edited.records[0].revisions[-2].disposition == "superseded"
    candidate = edited.records[0].revisions[-1]
    assert candidate.disposition == "pending"
    assert candidate.expected_accepted_revision == winner.revision_id
    assert edited.records[0].accepted_revision == winner.revision_id


@pytest.mark.parametrize("accept_first", [False, True])
def test_correction_accept_and_reject_batch_is_independent_of_selection_order(
    accept_first,
):
    snapshot, ids = import_candidates(MemorySnapshot(), document(), PROVENANCE, {})
    initial = ids[0]
    snapshot = accept_batch(
        snapshot,
        (
            Decision(
                initial.record_id,
                initial.revision_id,
                initial.candidate_digest,
                None,
                "accept",
            ),
        ),
    )
    proposals = tuple(
        LessonProposal(
            title="Protected reads",
            body=body,
            corrects_record=initial.record_id,
            expected_accepted_revision=initial.revision_id,
        )
        for body in ("Winner", "Alternative")
    )
    pending, corrections = import_candidates(
        snapshot,
        CandidateDocument(schema_version=SCHEMA_VERSION, lessons=proposals),
        PROVENANCE,
        {},
    )
    winner, loser = corrections
    accept = Decision(
        winner.record_id,
        winner.revision_id,
        winner.candidate_digest,
        initial.revision_id,
        "accept",
    )
    reject = Decision(
        loser.record_id,
        loser.revision_id,
        loser.candidate_digest,
        initial.revision_id,
        "reject",
    )
    updated = accept_batch(
        pending, (accept, reject) if accept_first else (reject, accept)
    )
    assert updated.records[0].accepted_revision == winner.revision_id
    assert updated.records[0].revisions[-1].disposition == "rejected"
    with pytest.raises(ValueError, match="Multiple accepted"):
        accept_batch(
            pending,
            (
                accept,
                Decision(
                    loser.record_id,
                    loser.revision_id,
                    loser.candidate_digest,
                    initial.revision_id,
                    "accept",
                ),
            ),
        )
