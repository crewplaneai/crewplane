from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from crewplane.artifacts.memory.store import MemoryStore
from crewplane.cli.app import app
from crewplane.core.memory.candidates import LessonProposal
from tests.helpers.memory import lesson_document
from tests.helpers.platforms import requires_project_memory


@requires_project_memory
def test_explicit_commands_lifecycle_and_read_only_inspection(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    missing = runner.invoke(app, ["memory", "list"])
    assert missing.exit_code == 1
    assert not (tmp_path / ".crewplane").exists()
    assert runner.invoke(app, ["memory", "init"]).exit_code == 0
    document = tmp_path / "lesson.json"
    document.write_text(
        lesson_document(
            LessonProposal(title="Protected reads", body="Preserve symlink checks.")
        ).model_dump_json(),
        encoding="utf-8",
        newline="\n",
    )
    proposed = runner.invoke(app, ["memory", "propose", "--file", str(document)])
    assert proposed.exit_code == 0, proposed.output
    store = MemoryStore(tmp_path)
    record = asyncio.run(store.snapshot()).records[0]
    revision = record.revisions[0]
    before = store.path.read_bytes()
    for args in (
        ["memory", "list"],
        ["memory", "show", record.record_id],
        ["memory", "search", "symlink"],
        ["memory", "review"],
    ):
        inspected = runner.invoke(app, args)
        assert inspected.exit_code == 0, inspected.output
    assert store.path.read_bytes() == before
    assert runner.invoke(app, ["memory", "accept", record.record_id]).exit_code != 0
    accepted = runner.invoke(
        app,
        [
            "memory",
            "accept",
            record.record_id,
            "--revision",
            revision.revision_id,
            "--digest",
            revision.candidate_digest,
            "--expected-accepted",
            "none",
        ],
    )
    assert accepted.exit_code == 0, accepted.output
    assert (
        "historical evidence"
        in runner.invoke(app, ["memory", "search", "symlink"]).output
    )
    assert (
        runner.invoke(
            app,
            [
                "memory",
                "forget",
                record.record_id,
                "--state-digest",
                record.state_digest,
            ],
        ).exit_code
        == 1
    )
    current = asyncio.run(store.snapshot()).records[0]
    forgotten = runner.invoke(
        app,
        ["memory", "forget", record.record_id, "--state-digest", current.state_digest],
    )
    assert forgotten.exit_code == 0, forgotten.output
    assert not asyncio.run(store.snapshot()).records
    assert (
        runner.invoke(app, ["memory", "propose", "--file", str(document)]).exit_code
        == 0
    )
    assert asyncio.run(store.snapshot()).records[0].record_id != record.record_id


@pytest.mark.parametrize(
    "payload",
    [
        {"lessons": []},
        {"schema_version": "wrong", "lessons": []},
        {
            "schema_version": "2",
            "lessons": [{"title": "Forge", "body": "bad", "acceptance": {}}],
        },
    ],
)
def test_invalid_manual_documents_do_not_initialize(
    tmp_path: Path, monkeypatch, payload
):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    result = CliRunner().invoke(app, ["memory", "propose", "--file", str(path)])
    assert result.exit_code == 1
    assert not (tmp_path / ".crewplane").exists()


@requires_project_memory
def test_scripted_rejection_and_reimport_preserve_rejected_identity(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    assert runner.invoke(app, ["memory", "init"]).exit_code == 0
    proposal = tmp_path / "lesson.json"
    proposal.write_text(
        lesson_document(
            LessonProposal(title="Paths", body="Check current paths.")
        ).model_dump_json(),
        encoding="utf-8",
        newline="\n",
    )
    assert (
        runner.invoke(app, ["memory", "propose", "--file", str(proposal)]).exit_code
        == 0
    )
    record = asyncio.run(MemoryStore(tmp_path).snapshot()).records[0]
    revision = record.revisions[0]
    arguments = [
        record.record_id,
        "--revision",
        revision.revision_id,
        "--digest",
        revision.candidate_digest,
        "--expected-accepted",
        "none",
    ]
    rejected = runner.invoke(app, ["memory", "reject", *arguments])
    assert rejected.exit_code == 0, rejected.output
    repeated = runner.invoke(app, ["memory", "propose", "--file", str(proposal)])
    assert repeated.exit_code == 0 and "Pending" not in repeated.output
    snapshot = asyncio.run(MemoryStore(tmp_path).snapshot())
    assert snapshot.records[0].record_id == record.record_id
    assert snapshot.records[0].revisions[0].revision_id == revision.revision_id
    assert snapshot.records[0].revisions[0].disposition == "rejected"
    assert runner.invoke(app, ["memory", "accept", *arguments]).exit_code == 1
