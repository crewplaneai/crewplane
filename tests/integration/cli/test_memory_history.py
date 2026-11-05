from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml
from typer.testing import CliRunner

from crewplane.artifacts.atomic import atomic_write_json
from crewplane.artifacts.locks import acquire_same_context_lock
from crewplane.artifacts.memory.receipts import read_extraction
from crewplane.artifacts.memory.store import MemoryStore
from crewplane.artifacts.naming import node_state_relative_path
from crewplane.cli.app import app
from crewplane.cli.memory import extraction
from crewplane.cli.memory.extraction import extract_memory, retained_extraction_result
from crewplane.cli.memory.history import completed_parent
from crewplane.core.memory.candidates import LessonProposal
from tests.helpers.memory import lesson_document
from tests.helpers.memory_runtime import setup_parent


def test_explicit_extract_by_run_id_reuses_saved_result_without_parent_changes(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    config, parent, _packet, fixtures, _console = setup_parent(tmp_path)
    (fixtures / "extract.md").write_text(
        "```crewplane-memory-candidates\n"
        + lesson_document().model_dump_json()
        + "\n```",
        encoding="utf-8",
        newline="\n",
    )
    config_path = tmp_path / ".crewplane/config.yml"
    config_path.write_text(
        yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8", newline="\n"
    )
    before = (parent.stages_dir / "manifests/run.json").read_bytes()
    runner = CliRunner()
    result = runner.invoke(app, ["memory", "extract", "--run", parent.plan.run_id])
    assert result.exit_code == 0, result.output
    assert "no useful lesson" in result.output
    retry = runner.invoke(app, ["memory", "extract", "--run", parent.plan.run_key_name])
    assert retry.exit_code == 0, retry.output
    assert len(list((tmp_path / ".crewplane/execution-stages").iterdir())) == 2
    assert (parent.stages_dir / "manifests/run.json").read_bytes() == before


def test_historical_parent_requires_matching_terminal_manifest_and_released_lock(
    tmp_path: Path,
):
    _config, parent, _packet, _fixtures, _console = setup_parent(tmp_path)
    assert completed_parent(tmp_path, parent.plan.run_key_name).can_learn
    for selector in ("", "../outside", "missing", "a\\b"):
        with pytest.raises(ValueError):
            completed_parent(tmp_path, selector)
    lock = acquire_same_context_lock(parent.state_dir, "other", "identity", "signature")
    try:
        lock.update_run(parent.plan.run_id, parent.plan.run_key_name)
        with pytest.raises(ValueError, match="release is unconfirmed"):
            completed_parent(tmp_path, parent.plan.run_key_name)
    finally:
        assert lock.release()
    path = parent.stages_dir / "manifests/run.json"
    payload = json.loads(path.read_bytes())
    payload["workflow_signature"] = "0" * 64
    atomic_write_json(path, payload)
    with pytest.raises(ValueError, match="matching completed"):
        completed_parent(tmp_path, parent.plan.run_key_name)


def test_retained_extraction_cannot_rebind_to_different_packet(tmp_path: Path):
    config, parent, packet, fixtures, console = setup_parent(tmp_path)
    (fixtures / "extract.md").write_text(
        "```crewplane-memory-candidates\n"
        + lesson_document().model_dump_json()
        + "\n```",
        encoding="utf-8",
        newline="\n",
    )
    asyncio.run(extract_memory(config, parent, console))
    receipt = read_extraction(parent.stages_dir)
    changed = packet.model_copy(update={"captured_at": "different"})
    with pytest.raises(ValueError, match="saved host evidence packet"):
        retained_extraction_result(parent.state_dir, receipt, changed)
    with pytest.raises(ValueError, match="retained run reference"):
        retained_extraction_result(
            parent.state_dir,
            receipt.model_copy(update={"extraction_run_key": None}),
            packet,
        )
    with pytest.raises(ValueError, match="does not match"):
        retained_extraction_result(
            parent.state_dir,
            receipt.model_copy(update={"extraction_run_id": "wrong"}),
            packet,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "another-child-run"),
        ("run_key_name", "another-child-run-key"),
        ("workflow_signature", "0" * 64),
    ],
)
def test_retained_extraction_retry_rejects_foreign_node_state(
    tmp_path: Path, monkeypatch, field, value
):
    config, parent, packet, fixtures, console = setup_parent(tmp_path)
    document = lesson_document(
        LessonProposal(
            title="Protected reads",
            body="Preserve protected source reads.",
            evidence_ids=(packet.evidence[0].evidence_id,),
        )
    )
    (fixtures / "extract.md").write_text(
        "```crewplane-memory-candidates\n" + document.model_dump_json() + "\n```",
        encoding="utf-8",
        newline="\n",
    )
    with monkeypatch.context() as patch:
        patch.setattr(
            extraction,
            "import_extraction",
            AsyncMock(side_effect=OSError("retry import")),
        )
        first = asyncio.run(extract_memory(config, parent, console))
    assert "import failed" in first.message
    receipt = read_extraction(parent.stages_dir)
    child = parent.state_dir / "execution-stages" / receipt.extraction_run_key
    state_path = child / node_state_relative_path("extract")
    state = json.loads(state_path.read_bytes())
    state[field] = value
    atomic_write_json(state_path, state)
    before = (parent.stages_dir / "manifests/memory/extraction.json").read_bytes()
    with pytest.raises(ValueError, match="node state identity"):
        asyncio.run(extract_memory(config, parent, console))
    assert (
        parent.stages_dir / "manifests/memory/extraction.json"
    ).read_bytes() == before
    assert not asyncio.run(MemoryStore(tmp_path).snapshot()).records
    assert len(list((parent.state_dir / "execution-stages").iterdir())) == 2
