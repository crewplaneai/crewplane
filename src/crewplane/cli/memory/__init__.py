from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from crewplane.architecture.safe_file_reads import read_contained_bytes
from crewplane.artifacts.memory.store import MemoryStore
from crewplane.core.memory.candidates import CandidateDocument, LessonProposal
from crewplane.core.memory.lifecycle import Decision, find_record
from crewplane.core.memory.query import QueryContribution, build_query
from crewplane.core.memory.retrieval import select_memory
from crewplane.version import SCHEMA_VERSION

from .inspection import original_status
from .lifecycle import decide, forget, propose_document
from .rendering import print_literal, revision_card
from .review import interactive, review_candidates

memory_app = typer.Typer(
    help="Initialize, inspect, and explicitly review project-local memory."
)


def run_command[T](operation: Coroutine[Any, Any, T]) -> T:
    try:
        return asyncio.run(operation)
    except (OSError, ValueError, TimeoutError) as exc:
        print_literal(Console(stderr=True), f"Memory error: {exc}")
        raise typer.Exit(1) from exc


@memory_app.command("init")
def initialize() -> None:
    """Explicitly initialize the store and persistent lock without overwriting state."""
    snapshot = run_command(MemoryStore(Path.cwd()).initialize())
    print_literal(
        Console(),
        f"Project memory initialized; {len(snapshot.records)} records retained.",
    )


@memory_app.command()
def propose(
    file: Annotated[
        Path | None,
        typer.Option(
            "--file",
            help="Strict candidate JSON document; imported as human-authored, pending lessons.",
        ),
    ] = None,
) -> None:
    """Author pending lessons; never accept them implicitly."""
    console = Console()
    try:
        if file is not None:
            document = CandidateDocument.model_validate_json(
                read_contained_bytes(file.absolute().parent, file.name, 64 * 1024)
            )
        else:
            if not interactive(console):
                raise ValueError(
                    "Interactive proposal requires input and output terminals; use --file instead."
                )
            document = CandidateDocument(
                schema_version=SCHEMA_VERSION,
                lessons=(
                    LessonProposal(
                        title=typer.prompt("Title"),
                        body=typer.prompt("Complete lesson"),
                        qualifications=tuple(
                            filter(
                                None,
                                (
                                    typer.prompt(
                                        "Conditions / uncertainty (optional)",
                                        default="",
                                    ),
                                ),
                            )
                        ),
                    ),
                ),
            )
    except (OSError, ValueError) as exc:
        print_literal(console, str(exc))
        raise typer.Exit(1) from exc
    identities = run_command(propose_document(Path.cwd(), document))
    for identity in identities:
        print_literal(
            console,
            f"Candidate {identity.record_id}/{identity.revision_id}\nDigest: {identity.candidate_digest}\nExpected accepted: {identity.expected_accepted_revision or 'none'}",
        )
    if not identities:
        print_literal(console, "Valid proposal document: no lessons to import.")


@memory_app.command("list")
def list_records(pending: bool = False) -> None:
    """List retained records without invoking a provider."""
    snapshot = run_command(MemoryStore(Path.cwd()).snapshot())
    for record in snapshot.records:
        for revision in record.revisions:
            if not pending or revision.disposition == "pending":
                print_literal(
                    Console(),
                    f"{record.record_id}/{revision.revision_id} [{revision.disposition}] {revision.title}",
                )


@memory_app.command()
def show(record_id: str) -> None:
    """Show complete revisions and retained versus original evidence."""
    snapshot = run_command(MemoryStore(Path.cwd()).snapshot())
    try:
        record = find_record(snapshot.records, record_id)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console = Console()
    print_literal(console, f"Record state digest: {record.state_digest}")
    for revision in record.revisions:
        print_literal(console, revision_card(record, revision))
        for evidence in revision.evidence:
            print_literal(
                console,
                f"Original {evidence.evidence_id}: {original_status(Path.cwd(), evidence)}",
            )


@memory_app.command()
def search(
    query: str,
    max_records: Annotated[int, typer.Option(min=1)] = 5,
    max_chars: Annotated[int, typer.Option(min=1)] = 8000,
) -> None:
    """Inspect deterministic lexical recall, including no-match/no-fit reasons."""
    snapshot = run_command(MemoryStore(Path.cwd()).snapshot())
    selection = select_memory(
        snapshot,
        "inspection",
        "executor",
        build_query((QueryContribution(kind="literal", source="search", text=query),)),
        max_records,
        max_chars,
    )
    print_literal(Console(), selection.rendered or "No memory context selected.")
    for omission in selection.omissions:
        print_literal(Console(), omission)


@memory_app.command()
def accept(
    record_id: str,
    revision: Annotated[str, typer.Option("--revision")],
    digest: Annotated[str, typer.Option("--digest")],
    expected_accepted: Annotated[
        str,
        typer.Option(
            "--expected-accepted",
            help="Exact inspected predecessor revision, or 'none'.",
        ),
    ],
) -> None:
    """Accept exactly the inspected candidate revision and predecessor."""
    run_command(
        decide(
            Path.cwd(),
            (
                Decision(
                    record_id,
                    revision,
                    digest,
                    None if expected_accepted == "none" else expected_accepted,
                    "accept",
                ),
            ),
        )
    )
    print_literal(Console(), "Accepted the specified revision.")


@memory_app.command()
def reject(
    record_id: str,
    revision: Annotated[str, typer.Option("--revision")],
    digest: Annotated[str, typer.Option("--digest")],
    expected_accepted: Annotated[str, typer.Option("--expected-accepted")],
) -> None:
    """Reject exactly the inspected candidate; unchecked review cards are not rejected."""
    run_command(
        decide(
            Path.cwd(),
            (
                Decision(
                    record_id,
                    revision,
                    digest,
                    None if expected_accepted == "none" else expected_accepted,
                    "reject",
                ),
            ),
        )
    )
    print_literal(Console(), "Rejected the specified revision.")


@memory_app.command("forget")
def forget_command(
    record_id: str,
    state_digest: Annotated[
        str,
        typer.Option(
            "--state-digest",
            help="Complete record state digest printed by memory show.",
        ),
    ],
) -> None:
    """Delete the inspected record, revisions, evidence, and proposal identities."""
    run_command(forget(Path.cwd(), record_id, state_digest))
    print_literal(
        Console(),
        "Forgot the record. Existing plans, prompts, logs, backups and active invocations are unchanged.",
    )


@memory_app.command("review")
def review_command(
    run: Annotated[
        str | None,
        typer.Option(
            "--run", help="Parent run ID to review; never invokes extraction."
        ),
    ] = None,
) -> None:
    """Inspect, edit, explicitly accept/reject, or defer pending candidates."""
    run_command(review_candidates(Path.cwd(), Console(), run))


@memory_app.command()
def extract(
    run: Annotated[
        str,
        typer.Option("--run", help="Completed parent run ID or exact artifact key."),
    ],
    regenerate: bool = False,
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Retry retained extraction; --regenerate explicitly requests a new model call."""
    from crewplane.core.config import load_config

    from .extraction import extract_memory
    from .history import completed_parent

    console = Console()

    async def execute() -> None:
        parent = completed_parent(Path.cwd(), run)
        settings = load_config(config or Path.cwd() / ".crewplane" / "config.yml")
        print_literal(
            console,
            f"Retained parent {parent.plan.run_id if parent.plan else run}: {parent.outcome}.",
        )
        result = await extract_memory(settings, parent, console, regenerate)
        print_literal(console, result.message)
        if result.candidates:
            print_literal(
                console,
                f"Review pending lessons: crewplane memory review --run {parent.plan.run_id if parent.plan else run}",
            )

    run_command(execute())
