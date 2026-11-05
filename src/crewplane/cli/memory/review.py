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
from crewplane.core.memory.models import MemorySnapshot

from .inspection import related_records
from .lifecycle import decide
from .rendering import print_literal, revision_card


def interactive(console: Console) -> bool:
    return sys.stdin.isatty() and console.is_terminal


async def review_candidates(
    project_root: Path,
    console: Console,
    run_id: str | None = None,
    record_ids: frozenset[str] | None = None,
) -> None:
    if not interactive(console):
        print_literal(
            console,
            "Memory candidates remain pending. Review in an interactive terminal: crewplane memory review"
            + (f" --run {run_id}" if run_id else ""),
        )
        return
    store = MemoryStore(project_root)
    choices: dict[tuple[str, str], Decision] = {}
    try:
        while True:
            snapshot = await store.snapshot()
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
            pending = [
                (record, revision)
                for record in snapshot.records
                for revision in record.revisions
                if revision.disposition == "pending"
                and (run_id is None or revision.provenance.run_id == run_id)
                and (record_ids is None or record.record_id in record_ids)
            ]
            if not pending:
                print_literal(console, "No pending memory candidates in this scope.")
                return
            print_literal(
                console,
                "Memory review: no candidates selected by default. Unchecked means deferred.",
            )
            for index, (record, revision) in enumerate(pending, 1):
                action = choices.get((record.record_id, revision.revision_id))
                print_literal(
                    console,
                    f"{index}. [{action.action if action else ' '}] {revision.title} ({record.record_id}/{revision.revision_id})",
                )
            answer = typer.prompt(
                "inspect N / edit N / accept N / reject N / defer N / apply / quit",
                default="quit",
            ).strip()
            if answer == "quit":
                return
            if answer == "apply":
                if not choices:
                    print_literal(
                        console, "Nothing selected; all candidates remain pending."
                    )
                    continue
                try:
                    await decide(project_root, tuple(choices.values()))
                except ValueError as exc:
                    print_literal(
                        console,
                        f"Conflict; nothing committed. Choices will be refreshed; inspect changed revisions again: {exc}",
                    )
                    continue
                print_literal(
                    console,
                    f"Applied {len(choices)} explicit memory decisions atomically.",
                )
                return
            action, separator, number = answer.partition(" ")
            if (
                not separator
                or not number.isdecimal()
                or not 1 <= int(number) <= len(pending)
            ):
                print_literal(
                    console, "Choose an action and a displayed candidate number."
                )
                continue
            record, revision = pending[int(number) - 1]
            key = (record.record_id, revision.revision_id)
            if action == "defer":
                choices.pop(key, None)
                continue
            if action not in {"inspect", "edit", "accept", "reject"}:
                print_literal(console, "Unknown review action.")
                continue
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
            elif action == "edit":
                body = click.edit(revision.body)
                if body is None:
                    continue
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
                    corrects_record=record.record_id
                    if record.accepted_revision
                    else None,
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
                    print_literal(
                        console, f"Edit conflict; inspect the current candidate: {exc}"
                    )
                choices.pop(key, None)
                print_literal(
                    console, "Returned to preview; edited revisions are not selected."
                )
    except (EOFError, typer.Abort, KeyboardInterrupt):
        print_literal(
            console, "Review deferred; no unsubmitted decisions were applied."
        )
