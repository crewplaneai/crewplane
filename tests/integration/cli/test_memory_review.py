from __future__ import annotations

import asyncio
import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest
from rich.console import Console

from crewplane.artifacts.memory.store import MemoryStore
from crewplane.cli.memory.lifecycle import decide, forget, propose_document
from crewplane.cli.memory.review import review_candidates
from crewplane.core.memory.candidates import LessonProposal
from crewplane.core.memory.lifecycle import Decision, edit_candidate
from crewplane.core.memory.models import MemorySnapshot
from tests.helpers.memory import accepted_snapshot, initialize_memory, lesson_document


def review_setup(tmp_path: Path, monkeypatch):
    initialize_memory(tmp_path)
    document = lesson_document(
        LessonProposal(
            title="[red]Hostile\x1b[2J\u202e title",
            body="Protect path checks.\x07",
            qualifications=("Only current paths.",),
        )
    )
    asyncio.run(propose_document(tmp_path, document))
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=False, color_system=None)
    monkeypatch.setattr(
        "crewplane.cli.memory.review.interactive", Mock(return_value=True)
    )
    return MemoryStore(tmp_path), console, stream


def test_review_starts_unselected_and_defers_on_eof(tmp_path: Path, monkeypatch):
    store, console, stream = review_setup(tmp_path, monkeypatch)
    before = store.path.read_bytes()
    monkeypatch.setattr(
        "crewplane.cli.memory.review.typer.prompt",
        Mock(side_effect=["apply", "inspect 1", EOFError]),
    )
    asyncio.run(review_candidates(tmp_path, console))
    assert store.path.read_bytes() == before
    assert "Nothing selected" in stream.getvalue()
    assert "\\u001b" in stream.getvalue()
    assert "\\u202e" in stream.getvalue()
    assert "\x1b" not in stream.getvalue()
    assert "\x07" not in stream.getvalue()


@pytest.mark.parametrize("action", ["accept", "reject", "defer"])
def test_explicit_review_decisions_not_unchecked_cards(
    tmp_path: Path, monkeypatch, action
):
    store, console, _stream = review_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "crewplane.cli.memory.review.typer.prompt",
        Mock(side_effect=[f"{action} 1", "apply", "quit"]),
    )
    monkeypatch.setattr(
        "crewplane.cli.memory.review.typer.confirm", Mock(return_value=True)
    )
    asyncio.run(review_candidates(tmp_path, console))
    record = asyncio.run(store.snapshot()).records[0]
    assert (
        record.revisions[0].disposition
        == {"accept": "accepted", "reject": "rejected", "defer": "pending"}[action]
    )


def test_edit_creates_revision_and_returns_unselected_to_preview(
    tmp_path: Path, monkeypatch
):
    store, console, stream = review_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "crewplane.cli.memory.review.typer.prompt",
        Mock(side_effect=["edit 1", "Revised title", "apply", "accept 1", "apply"]),
    )
    monkeypatch.setattr(
        "crewplane.cli.memory.review.typer.confirm", Mock(return_value=True)
    )
    monkeypatch.setattr(
        "crewplane.cli.memory.review.click.edit",
        Mock(return_value="New complete lesson."),
    )
    asyncio.run(review_candidates(tmp_path, console))
    record = asyncio.run(store.snapshot()).records[0]
    assert len(record.revisions) == 2
    assert record.revisions[0].disposition == "superseded"
    assert record.revisions[1].body == "New complete lesson."
    assert record.accepted_revision == record.revisions[1].revision_id
    assert "Nothing selected" in stream.getvalue()
    assert "Returned to preview" in stream.getvalue()


@pytest.mark.parametrize("action", ["inspect", "accept"])
@pytest.mark.parametrize("field", ["qualifications", "tags"])
@pytest.mark.parametrize("replacement", [(), ("Changed applicability",)])
def test_correction_review_compares_semantic_fields_before_confirmation(
    tmp_path: Path, monkeypatch, action, field, replacement
):
    restriction = "Only replace caches inside the local project; never shared caches."
    original = LessonProposal(
        title="Cache repair",
        body="Replace the cache when its digest is invalid.",
        tags=("local-only",),
        qualifications=(restriction,),
    )
    initial = initialize_memory(tmp_path, accepted_snapshot(original)).records[0]
    proposed = LessonProposal.model_validate(
        original.model_dump()
        | {
            field: replacement,
            "corrects_record": initial.record_id,
            "expected_accepted_revision": initial.accepted_revision,
        }
    )
    asyncio.run(propose_document(tmp_path, lesson_document(proposed)))
    store = MemoryStore(tmp_path)
    before = store.path.read_bytes()
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=False, color_system=None, width=240)
    monkeypatch.setattr(
        "crewplane.cli.memory.review.interactive", Mock(return_value=True)
    )
    monkeypatch.setattr(
        "crewplane.cli.memory.review.typer.prompt",
        Mock(side_effect=[f"{action} 1", "quit"]),
    )
    label, removed = (
        ("Qualification", restriction)
        if field == "qualifications"
        else ("Tags", "local-only")
    )

    def assert_comparison_visible(*args, **kwargs):
        del args, kwargs
        assert f"-{label}: {removed}" in stream.getvalue()
        return False

    confirm = Mock(side_effect=assert_comparison_visible)
    monkeypatch.setattr("crewplane.cli.memory.review.typer.confirm", confirm)

    asyncio.run(review_candidates(tmp_path, console))

    assert store.path.read_bytes() == before
    comparison = stream.getvalue().split(
        "Correction diff (displayed predecessor / proposed):", 1
    )[1]
    assert f"--- {initial.accepted_revision}" in comparison
    assert f"-{label}: {removed}" in comparison
    if replacement:
        assert f"+{label}: {replacement[0]}" in comparison
    assert confirm.call_count == (1 if action == "accept" else 0)


@pytest.mark.race
def test_review_conflict_commits_nothing_and_preserves_concurrent_decision(
    tmp_path: Path, monkeypatch
):
    store, console, stream = review_setup(tmp_path, monkeypatch)
    initial = asyncio.run(store.snapshot()).records[0]
    revision = initial.revisions[0]
    answers = iter(["accept 1", "apply", "quit"])
    calls = 0

    def reject_elsewhere():
        asyncio.run(
            decide(
                tmp_path,
                (
                    Decision(
                        initial.record_id,
                        revision.revision_id,
                        revision.candidate_digest,
                        None,
                        "reject",
                    ),
                ),
            )
        )

    def answer(*args, **kwargs):
        nonlocal calls
        del args, kwargs
        calls += 1
        if calls == 2:
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(reject_elsewhere).result(timeout=5)
        return next(answers)

    monkeypatch.setattr("crewplane.cli.memory.review.typer.prompt", answer)
    monkeypatch.setattr(
        "crewplane.cli.memory.review.typer.confirm", Mock(return_value=True)
    )
    asyncio.run(review_candidates(tmp_path, console))
    assert (
        asyncio.run(store.snapshot()).records[0].revisions[0].disposition == "rejected"
    )
    assert "Conflict; nothing committed" in stream.getvalue()


@pytest.mark.race
@pytest.mark.parametrize("concurrent_action", ["accept", "reject", "edit", "forget"])
def test_review_conflict_retires_removed_selection_and_preserves_other_choices(
    tmp_path: Path, monkeypatch, concurrent_action: str
):
    store, console, stream = review_setup(tmp_path, monkeypatch)
    console.width = 200
    asyncio.run(
        propose_document(
            tmp_path, lesson_document(LessonProposal(title="Second", body="Keep me."))
        )
    )
    first, second = asyncio.run(store.snapshot()).records
    revision = first.revisions[0]
    selected = Decision(
        first.record_id,
        revision.revision_id,
        revision.candidate_digest,
        None,
        "reject" if concurrent_action == "reject" else "accept",
    )
    answers = iter(["accept 1", "accept 2", "apply", "apply", "quit"])
    calls = 0

    async def change_elsewhere():
        if concurrent_action == "forget":
            await forget(tmp_path, first.record_id, first.state_digest)
        elif concurrent_action == "edit":

            def mutate(current: MemorySnapshot) -> tuple[MemorySnapshot, None]:
                return (
                    edit_candidate(
                        current,
                        selected,
                        LessonProposal(title="Edited", body="New candidate."),
                    ),
                    None,
                )

            await store.transact(mutate)
        else:
            await decide(tmp_path, (selected,))

    def answer(*args, **kwargs):
        nonlocal calls
        del args, kwargs
        calls += 1
        if calls == 3:
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(asyncio.run, change_elsewhere()).result(timeout=5)
        return next(answers)

    monkeypatch.setattr("crewplane.cli.memory.review.typer.prompt", answer)
    monkeypatch.setattr(
        "crewplane.cli.memory.review.typer.confirm", Mock(return_value=True)
    )
    asyncio.run(review_candidates(tmp_path, console))
    records = {
        record.record_id: record for record in asyncio.run(store.snapshot()).records
    }
    assert records[second.record_id].revisions[0].disposition == "accepted"
    if concurrent_action == "forget":
        assert first.record_id not in records
    else:
        assert (
            records[first.record_id].revisions[0].disposition
            == {
                "accept": "accepted",
                "reject": "rejected",
                "edit": "superseded",
            }[concurrent_action]
        )
        if concurrent_action == "edit":
            assert records[first.record_id].revisions[1].disposition == "pending"
    assert "Conflict; nothing committed" in stream.getvalue()
    assert (
        f"Selection cleared for {first.record_id}/{revision.revision_id}"
        in stream.getvalue()
    )
    assert "Applied 1 explicit memory decisions atomically." in stream.getvalue()
