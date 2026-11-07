from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from unittest.mock import Mock

from typer.testing import CliRunner

import crewplane.cli.memory as memory_cli
from crewplane.artifacts.memory.store import MemoryStore
from crewplane.cli.app import app
from crewplane.cli.memory.inspection import original_status
from crewplane.core.memory.candidates import LessonProposal
from crewplane.core.memory.lifecycle import Decision, accept_batch, import_candidates
from crewplane.core.memory.models import MemorySnapshot, ProposalProvenance
from tests.helpers.memory import evidence_receipt, initialize_memory, lesson_document
from tests.helpers.platforms import requires_project_memory, symlink_or_skip

pytestmark = requires_project_memory


def test_archived_originals_do_not_invalidate_retained_accepted_recall(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "source.md"
    source.write_bytes(b"The agent reports protected reads were checked.")
    receipt = evidence_receipt(source.read_text()).model_copy(
        update={
            "source_locator": source.as_posix(),
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        }
    )
    pending, ids = import_candidates(
        MemorySnapshot(),
        lesson_document(
            LessonProposal(
                title="Protected reads",
                body="Verify current requirements before relying on the old check.",
                evidence_ids=(receipt.evidence_id,),
            )
        ),
        ProposalProvenance(
            author="extractor", source_identity="retained-packet", run_id="run"
        ),
        {receipt.evidence_id: receipt},
    )
    identity = ids[0]
    accepted = accept_batch(
        pending,
        (
            Decision(
                identity.record_id,
                identity.revision_id,
                identity.candidate_digest,
                None,
                "accept",
            ),
        ),
    )
    initialize_memory(tmp_path, accepted)
    store = MemoryStore(tmp_path)
    before = store.path.read_bytes()
    observed = []

    def inspect_without_store_lock(root, evidence):
        async def acquire_writer():
            def unchanged(snapshot):
                return snapshot, None

            await MemoryStore(root, 0.1).transact(unchanged)

        asyncio.run(acquire_writer())
        status = original_status(root, evidence)
        observed.append(status)
        return status

    monkeypatch.setattr(memory_cli, "original_status", inspect_without_store_lock)
    runner = CliRunner()
    assert runner.invoke(app, ["memory", "show", identity.record_id]).exit_code == 0
    assert observed[-1] == "available and unchanged"
    source.write_bytes(b"Changed original")
    assert (
        "changed (retained"
        in runner.invoke(app, ["memory", "show", identity.record_id]).output
    )
    source.unlink()
    result = runner.invoke(app, ["memory", "show", identity.record_id])
    assert result.exit_code == 0 and "missing (retained" in result.output
    assert receipt.excerpt in result.output
    recalled = runner.invoke(app, ["memory", "search", "protected reads"])
    assert recalled.exit_code == 0 and "Verify current requirements" in recalled.output
    assert store.path.read_bytes() == before
    target = tmp_path / "other.md"
    target.write_bytes(b"alias")
    symlink_or_skip(source, target)
    assert "unavailable or unsafe" in original_status(tmp_path, receipt)


def test_interactive_proposal_is_pending_and_noninteractive_requires_file(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    denied = runner.invoke(app, ["memory", "propose"])
    assert denied.exit_code == 1 and not (tmp_path / ".crewplane").exists()
    initialize_memory(tmp_path)
    monkeypatch.setattr(memory_cli, "interactive", Mock(return_value=True))
    result = runner.invoke(
        app,
        ["memory", "propose"],
        input="Protected reads\nPreserve safety checks.\nOnly for current files.\n",
    )
    assert result.exit_code == 0, result.output
    snapshot = asyncio.run(MemoryStore(tmp_path).snapshot())
    assert snapshot.records[0].accepted_revision is None
    assert snapshot.records[0].revisions[0].qualifications == (
        "Only for current files.",
    )
    assert not snapshot.records[0].revisions[0].evidence
