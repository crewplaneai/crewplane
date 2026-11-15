"""Real-process memory interleavings; stdin/stdout provide explicit barriers."""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

from crewplane.artifacts.locks import acquire_same_context_lock
from crewplane.artifacts.memory.store import MemoryStore
from crewplane.core.memory.candidates import CandidateDocument, LessonProposal
from crewplane.core.memory.lifecycle import import_candidates
from crewplane.core.memory.models import MemorySnapshot, ProposalProvenance
from crewplane.version import SCHEMA_VERSION


async def extract(mode: str, root: Path, payload: str) -> None:
    import io
    from unittest.mock import patch

    from rich.console import Console

    from crewplane.cli.memory import extraction
    from crewplane.cli.memory.history import completed_parent
    from crewplane.core.config import Config
    from crewplane.runtime.agent.process.drain import (
        ProcessDrainError,
        ProcessDrainEvidence,
    )
    from crewplane.runtime.execution import execute_workflow

    request = json.loads(payload)
    config = Config.model_validate(request["config"])
    parent = completed_parent(root, request["parent_key"])
    console = Console(file=io.StringIO(), force_terminal=False, color_system=None)
    original_run = extraction.execute_workflow_run
    original_save = extraction.save_extraction
    original_read = extraction.read_extraction
    publications = 0
    output = sys.stdout

    def read(stages):
        receipt = original_read(stages)
        print("read", file=output, flush=True)
        sys.stdin.readline()
        return receipt

    async def cleanup_failed(*args, **kwargs):
        await execute_workflow(*args, **kwargs)
        print("executed", file=output, flush=True)
        await asyncio.to_thread(sys.stdin.readline)
        raise ProcessDrainError(
            ProcessDrainEvidence(123, 123, True, False), "cleanup unconfirmed"
        )

    async def run_with_cleanup_failure(*args, **kwargs):
        kwargs["execute_workflow_impl"] = cleanup_failed
        return await original_run(*args, **kwargs)

    def save(attempt, receipt):
        nonlocal publications
        publications += 1
        if publications == 2:
            if mode == "extract_cleanup_publication":
                raise OSError("terminal receipt unavailable before replacement")
            if mode == "extract_contender":
                print("terminal_waiting", file=output, flush=True)
                sys.stdin.readline()
        original_save(attempt, receipt)

    sys.stdin.readline()
    with (
        redirect_stdout(sys.stderr),
        patch.object(extraction, "save_extraction", new=save),
        patch.object(extraction, "read_extraction", new=read),
        patch.object(
            extraction,
            "execute_workflow_run",
            new=original_run
            if mode == "extract_contender"
            else run_with_cleanup_failure,
        ),
    ):
        try:
            result = await extraction.extract_memory(config, parent, console)
            print(f"result:{result.message}", file=output, flush=True)
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"error:{type(exc).__name__}", file=output, flush=True)


async def main() -> None:
    mode, root, title = sys.argv[1:]
    store = MemoryStore(Path(root), lock_timeout_seconds=5)
    print("ready", flush=True)
    if mode.startswith("extract_"):
        await extract(mode, Path(root), title)
    elif mode == "execution":
        identity = json.loads(title)
        lock = acquire_same_context_lock(Path(root), **identity)
        print("locked", flush=True)
        try:
            sys.stdin.readline()
        finally:
            assert lock.release()
    elif mode == "initialize":
        sys.stdin.readline()
        await store.initialize()
    else:

        def mutate(snapshot: MemorySnapshot):
            print("locked", flush=True)
            sys.stdin.readline()
            document = CandidateDocument(
                schema_version=SCHEMA_VERSION,
                lessons=(
                    LessonProposal(title=title, body="A complete independent lesson."),
                ),
            )
            return import_candidates(
                snapshot,
                document,
                ProposalProvenance(author="human", source_identity=title),
                {},
            )

        print("attempting", flush=True)
        await store.transact(mutate)
    print("done", flush=True)


asyncio.run(main())
