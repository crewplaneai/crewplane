from __future__ import annotations

import io

import pytest
from rich.console import Console

from crewplane.cli.memory.rendering import (
    print_literal,
    revision_card,
    revision_content,
)
from crewplane.core.memory.candidates import LessonProposal
from crewplane.core.memory.lifecycle import Decision, accept_batch, import_candidates
from crewplane.core.memory.models import MemorySnapshot, ProposalProvenance
from tests.helpers.memory import accepted_snapshot, evidence_receipt, lesson_document


def test_print_literal_escapes_controls_and_preserves_markup_text():
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=True, color_system="standard")

    result = print_literal(console, "[red]True 123[/red]\x1b[2J\u202e\x07\r\nnext")

    assert result is None
    assert stream.getvalue() == (
        "[red]True 123[/red]\\u001b[2J\\u202e\\u0007\\u000d\nnext\n"
    )


@pytest.mark.parametrize("tags", [(), ("z", "a")])
def test_revision_content_and_card_preserve_metadata_order(tags):
    record = accepted_snapshot(
        LessonProposal(
            title="[red]Title\x07",
            body="First line\r\n\nLast line\n",
            tags=tags,
            qualifications=("Second limit.", "First limit."),
        )
    ).records[0]
    revision = record.revisions[0]
    before = record.model_dump_json()
    metadata = (["Tags: z, a"] if tags else []) + [
        "Qualification: Second limit.",
        "Qualification: First limit.",
    ]

    assert revision_content(revision) == [
        "[red]Title\x07",
        "First line",
        "",
        "Last line",
        *metadata,
    ]
    assert revision_card(record, revision) == "\n".join(
        [
            "[ ] [red]Title\x07",
            f"Record: {record.record_id}",
            f"Revision: {revision.revision_id} (accepted)",
            f"Candidate digest: {revision.candidate_digest}",
            "Expected accepted revision: none",
            f"Current accepted revision: {revision.revision_id}",
            "",
            "First line\r\n\nLast line\n",
            *metadata,
            "Authorship: human; test-author",
            "Evidence strength: human-authored; no host-verified execution evidence.",
        ]
    )
    assert record.model_dump_json() == before


def test_revision_card_preserves_evidence_details_and_order():
    first = evidence_receipt("First excerpt.\n").model_copy(
        update={
            "evidence_id": "e-first",
            "qualifications": ("Second limit.", "First limit."),
            "provenance": ("Second source.", "First source."),
        }
    )
    second = evidence_receipt("Second excerpt.").model_copy(
        update={"evidence_id": "e-second", "qualifications": ()}
    )
    snapshot, _ = import_candidates(
        MemorySnapshot(),
        lesson_document(
            LessonProposal(
                title="Title", body="Body", evidence_ids=("e-first", "e-second")
            )
        ),
        ProposalProvenance(author="extractor", source_identity="retained-run"),
        {"e-second": second, "e-first": first},
    )
    record = snapshot.records[0]
    revision = record.revisions[0]

    card = revision_card(record, revision)

    assert card.split("Authorship: extractor; retained-run\n", 1)[1] == (
        "Evidence e-first: agent_output\n"
        f"Source: {first.source_locator}\n"
        f"Source SHA256: {first.source_sha256}\n"
        f"Observed: {first.observed_at}; retained: {first.captured_at}\n"
        "Retained excerpt:\nFirst excerpt.\n\n"
        "Outcome limitation: Second limit.\n"
        "Outcome limitation: First limit.\n"
        "Provenance: Second source.\n"
        "Provenance: First source.\n"
        "Evidence e-second: agent_output\n"
        f"Source: {second.source_locator}\n"
        f"Source SHA256: {second.source_sha256}\n"
        f"Observed: {second.observed_at}; retained: {second.captured_at}\n"
        "Retained excerpt:\nSecond excerpt."
    )
    assert revision_content(revision) == ["Title", "Body"]


@pytest.mark.parametrize("body", ["Old body.", "New body."])
def test_revision_card_compares_displayed_predecessor_even_when_current_differs(body):
    snapshot = accepted_snapshot(LessonProposal(title="Title", body="Old body."))
    original = snapshot.records[0]
    snapshot, _ = import_candidates(
        snapshot,
        lesson_document(
            LessonProposal(
                title="Title",
                body=body,
                corrects_record=original.record_id,
                expected_accepted_revision=original.accepted_revision,
            )
        ),
        ProposalProvenance(author="human", source_identity="correction"),
        {},
    )
    revision = snapshot.records[0].revisions[-1]
    record = accept_batch(
        snapshot,
        (
            Decision(
                original.record_id,
                revision.revision_id,
                revision.candidate_digest,
                revision.expected_accepted_revision,
                "accept",
            ),
        ),
    ).records[0]
    revision = record.revisions[-1]

    card = revision_card(record, revision)

    expected_diff = "Correction diff (displayed predecessor / proposed):"
    if body == "New body.":
        expected_diff += (
            f"\n--- {original.accepted_revision}\n+++ {revision.revision_id}\n"
            "@@ -1,2 +1,2 @@\n Title\n-Old body.\n+New body."
        )
    assert card.endswith("\n" + expected_diff)


def test_revision_card_requires_correction_predecessor_in_supplied_record():
    record = accepted_snapshot(LessonProposal(title="Title", body="Body")).records[0]
    revision = record.revisions[0].model_copy(
        update={"expected_accepted_revision": "missing"}
    )

    with pytest.raises(StopIteration):
        revision_card(record, revision)
