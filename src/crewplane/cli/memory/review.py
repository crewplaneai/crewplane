"""Review pending memory revisions with blocking terminal prompts and an editor."""

from __future__ import annotations

import sys
from pathlib import Path

import click
import typer
from rich.console import Console

from crewplane.artifacts.memory.store import MemoryStore
from crewplane.core.memory.candidates import LessonProposal
from crewplane.core.memory.lifecycle import (
    Decision,
    checked_candidate,
    edit_candidate,
    find_record,
)
from crewplane.core.memory.models import MemoryRecord, MemoryRevision, MemorySnapshot

from .inspection import related_records
from .lifecycle import decide
from .rendering import print_literal, revision_card


def interactive(console: Console) -> bool:
    """Return whether stdin is a TTY and the console reports terminal output."""
    return sys.stdin.isatty() and console.is_terminal


async def review_candidates(
    project_root: Path,
    console: Console,
    run_id: str | None = None,
    record_ids: frozenset[str] | None = None,
) -> None:
    """Review pending candidates in store order within the optional filters.

    Both stdin and console output must be terminals; otherwise print a review
    command and return without opening memory. Despite being async, prompts and
    the editor deliberately block the caller's event-loop thread.

    Intersect run provenance and record ID filters when both are supplied. Edits
    persist immediately and return unselected; confirmed accept/reject choices
    remain in memory until an atomic apply. Refresh stale choices before each
    preview and recover from edit/apply ValueErrors without retrying a write.

    EOF, Typer abort, and keyboard interruption end review without submitting
    queued decisions; completed writes remain persisted. Other errors propagate,
    including store read failures and edit proposal validation errors.
    """
    if not interactive(console):
        print_literal(
            console,
            "Memory candidates remain pending. Review in an interactive terminal: crewplane memory review"
            + (f" --run {run_id}" if run_id else ""),
        )
        return
    store = MemoryStore(project_root)
    try:
        await _review_loop(store, console, run_id, record_ids)
    except (EOFError, typer.Abort, KeyboardInterrupt):
        print_literal(
            console, "Review deferred; no unsubmitted decisions were applied."
        )


async def _review_loop(
    store: MemoryStore,
    console: Console,
    run_id: str | None,
    record_ids: frozenset[str] | None,
) -> None:
    choices: dict[tuple[str, str], Decision] = {}
    while True:
        snapshot = await store.snapshot()
        _refresh_choices(snapshot, choices, console)
        pending = _pending_candidates(snapshot, run_id, record_ids)
        if not pending:
            print_literal(console, "No pending memory candidates in this scope.")
            return
        _print_preview(pending, choices, console)
        answer = typer.prompt(
            "inspect N / edit N / accept N / reject N / defer N / apply / quit",
            default="quit",
        ).strip()
        if answer == "quit":
            return
        if answer == "apply":
            if await _apply_choices(store.project_root, choices, console):
                return
            continue
        command = _parse_candidate_command(answer, pending, console)
        if command is not None:
            await _review_candidate(command, snapshot, store, choices, console)


def _refresh_choices(
    snapshot: MemorySnapshot,
    choices: dict[tuple[str, str], Decision],
    console: Console,
) -> None:
    for key, decision in tuple(choices.items()):
        try:
            checked_candidate(
                find_record(snapshot.records, decision.record_id),
                decision,
                decision.action == "accept",
            )
        except ValueError:
            choices.pop(key)
            print_literal(
                console,
                f"Selection cleared for {decision.record_id}/{decision.revision_id}: "
                "the revision changed or is no longer selectable. "
                "Inspect its current state before selecting again.",
            )


def _pending_candidates(
    snapshot: MemorySnapshot,
    run_id: str | None,
    record_ids: frozenset[str] | None,
) -> list[tuple[MemoryRecord, MemoryRevision]]:
    return [
        (record, revision)
        for record in snapshot.records
        for revision in record.revisions
        if revision.disposition == "pending"
        and (run_id is None or revision.provenance.run_id == run_id)
        and (record_ids is None or record.record_id in record_ids)
    ]


def _print_preview(
    pending: list[tuple[MemoryRecord, MemoryRevision]],
    choices: dict[tuple[str, str], Decision],
    console: Console,
) -> None:
    print_literal(
        console,
        "Memory review: no candidates selected by default. Unchecked means deferred.",
    )
    for index, (record, revision) in enumerate(pending, 1):
        selected_decision = choices.get((record.record_id, revision.revision_id))
        print_literal(
            console,
            f"{index}. [{selected_decision.action if selected_decision else ' '}] {revision.title} ({record.record_id}/{revision.revision_id})",
        )


async def _apply_choices(
    project_root: Path, choices: dict[tuple[str, str], Decision], console: Console
) -> bool:
    if not choices:
        print_literal(console, "Nothing selected; all candidates remain pending.")
        return False
    try:
        await decide(project_root, tuple(choices.values()))
    except ValueError as exc:
        print_literal(
            console,
            f"Conflict; nothing committed. Choices will be refreshed; inspect changed revisions again: {exc}",
        )
        return False
    print_literal(
        console, f"Applied {len(choices)} explicit memory decisions atomically."
    )
    return True


def _parse_candidate_command(
    answer: str, pending: list[tuple[MemoryRecord, MemoryRevision]], console: Console
) -> tuple[str, MemoryRecord, MemoryRevision] | None:
    action, separator, number = answer.partition(" ")
    if not separator or not number.isdecimal() or not 1 <= int(number) <= len(pending):
        print_literal(console, "Choose an action and a displayed candidate number.")
        return None
    record, revision = pending[int(number) - 1]
    if action not in {"inspect", "edit", "accept", "reject", "defer"}:
        print_literal(console, "Unknown review action.")
        return None
    return action, record, revision


async def _review_candidate(
    command: tuple[str, MemoryRecord, MemoryRevision],
    snapshot: MemorySnapshot,
    store: MemoryStore,
    choices: dict[tuple[str, str], Decision],
    console: Console,
) -> None:
    action, record, revision = command
    key = (record.record_id, revision.revision_id)
    if action == "defer":
        choices.pop(key, None)
        return
    print_literal(console, revision_card(record, revision))
    print_literal(
        console,
        "Related records: "
        + (", ".join(related_records(record, snapshot.records)) or "none"),
    )
    decision = Decision(
        record.record_id,
        revision.revision_id,
        revision.candidate_digest,
        revision.expected_accepted_revision
        if action == "accept"
        else record.accepted_revision,
        "reject" if action == "reject" else "accept",
    )
    if action in {"accept", "reject"}:
        if typer.confirm(
            f"Select this exact displayed revision for {action}?", default=False
        ):
            choices[key] = decision
    elif action == "edit" and await _edit_candidate(
        store, console, record, revision, decision
    ):
        choices.pop(key, None)
        print_literal(
            console, "Returned to preview; edited revisions are not selected."
        )


async def _edit_candidate(
    store: MemoryStore,
    console: Console,
    record: MemoryRecord,
    revision: MemoryRevision,
    decision: Decision,
) -> bool:
    """Return whether an edit was submitted, including a handled conflict."""
    body = click.edit(revision.body)
    if body is None:
        return False
    proposal = LessonProposal(
        title=typer.prompt(
            "New title (blank keeps displayed title)",
            default="",
            show_default=False,
        )
        or revision.title,
        body=body,
        tags=revision.tags,
        qualifications=revision.qualifications,
        evidence_ids=tuple(item.evidence_id for item in revision.evidence),
        corrects_record=record.record_id if record.accepted_revision else None,
        expected_accepted_revision=record.accepted_revision,
    )

    def mutate(
        current: MemorySnapshot,
        selected: Decision = decision,
        edited: LessonProposal = proposal,
    ) -> tuple[MemorySnapshot, None]:
        return edit_candidate(current, selected, edited), None

    try:
        await store.transact(mutate)
    except ValueError as exc:
        print_literal(console, f"Edit conflict; inspect the current candidate: {exc}")
    return True
