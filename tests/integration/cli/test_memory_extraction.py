from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from crewplane.artifacts.memory.receipts import read_extraction, read_packet
from crewplane.artifacts.memory.store import MemoryStore
from crewplane.cli.memory.extraction import extract_memory
from crewplane.core.memory.candidates import LessonProposal
from tests.helpers.memory import lesson_document
from tests.helpers.memory_runtime import setup_parent


@pytest.mark.parametrize("log_output", [False, True])
def test_normal_extraction_artifacts_opaque_evidence_and_idempotent_retry(
    tmp_path: Path, log_output
):
    config, parent, packet, fixtures, console = setup_parent(tmp_path, log_output)
    document = lesson_document(
        LessonProposal(
            title="Protected reads",
            body="Preserve protected source reads.",
            qualifications=("Check current requirements.",),
            evidence_ids=(packet.evidence[0].evidence_id,),
        )
    )
    (fixtures / "extract.md").write_text(
        "# Candidate results\n```crewplane-memory-candidates\n"
        + document.model_dump_json()
        + "\n```\n",
        encoding="utf-8",
        newline="\n",
    )
    (fixtures / "extract.mutations.json").write_text(
        json.dumps(
            {
                "required_prompt_contains": [
                    "{{env:DO_NOT_EXPAND}}",
                    "<!-- crewplane:reviewer -->literal",
                    packet.evidence[0].evidence_id,
                ]
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    before = (parent.stages_dir / "manifests/run.json").read_bytes()
    result = asyncio.run(extract_memory(config, parent, console))
    assert len(result.candidates) == 1, result.message
    receipt = read_extraction(parent.stages_dir)
    assert (
        receipt.provider_status
        == receipt.validation_status
        == receipt.import_status
        == "succeeded"
    )
    assert receipt.completion_eligible
    child = tmp_path / ".crewplane/execution-stages" / receipt.extraction_run_key
    plan = json.loads((child / "preflight/execution-plan.json").read_bytes())
    assert len(plan["nodes"]) == 1
    assert not plan["nodes"][0]["memory"]
    assert plan["nodes"][0]["provider_records"][0]["provider"] == "alpha"
    assert (
        "{{env:DO_NOT_EXPAND}}" in (child / "preflight/render-plans.json").read_text()
    )
    assert (child / "logs/events.ndjson").exists()
    assert (parent.stages_dir / "manifests/run.json").read_bytes() == before
    repeated = asyncio.run(extract_memory(config, parent, console))
    assert repeated.candidates == result.candidates
    assert len(list((tmp_path / ".crewplane/execution-stages").iterdir())) == 2
    snapshot = asyncio.run(MemoryStore(tmp_path).snapshot())
    assert snapshot.records[0].accepted_revision is None
    assert snapshot.records[0].revisions[0].evidence[0].kind == "agent_output"
    assert read_packet(parent.stages_dir) == packet


def test_malformed_success_is_reused_until_explicit_regeneration(tmp_path: Path):
    config, parent, packet, fixtures, console = setup_parent(tmp_path)
    (fixtures / "extract.md").write_text(
        "not a candidate block", encoding="utf-8", newline="\n"
    )
    first = asyncio.run(extract_memory(config, parent, console))
    assert "validation failed" in first.message
    receipt = read_extraction(parent.stages_dir)
    assert receipt.provider_status == "succeeded"
    assert receipt.validation_status == "failed"
    assert receipt.import_status == "not_attempted"
    (fixtures / "extract.md").write_text(
        "```crewplane-memory-candidates\n"
        + lesson_document().model_dump_json()
        + "\n```",
        encoding="utf-8",
        newline="\n",
    )
    retry = asyncio.run(extract_memory(config, parent, console))
    assert "validation failed" in retry.message
    assert len(list((tmp_path / ".crewplane/execution-stages").iterdir())) == 2
    regenerated = asyncio.run(extract_memory(config, parent, console, regenerate=True))
    assert regenerated.message.endswith("no useful lesson proposed.")
    assert len(list((tmp_path / ".crewplane/execution-stages").iterdir())) == 3
    assert read_packet(parent.stages_dir) == packet


def test_extraction_timeout_and_provider_failure_preserve_parent(tmp_path: Path):
    config, parent, _packet, fixtures, console = setup_parent(tmp_path)
    original = (parent.stages_dir / "manifests/run.json").read_bytes()
    failed = asyncio.run(extract_memory(config, parent, console))
    assert "failed" in failed.message
    assert read_extraction(parent.stages_dir).provider_status == "failed"
    (fixtures / "extract.md").write_text(
        "```crewplane-memory-candidates\n"
        + lesson_document().model_dump_json()
        + "\n```",
        encoding="utf-8",
        newline="\n",
    )
    config.settings.memory.extraction_timeout_seconds = 0.05
    config.settings.integrations.invoker.options["delay_seconds"] = 1
    timed = asyncio.run(extract_memory(config, parent, console))
    assert "timeout" in timed.message
    receipt = read_extraction(parent.stages_dir)
    assert receipt.provider_status == "timeout"
    assert receipt.extraction_run_key is not None
    child_manifest = json.loads(
        (
            tmp_path
            / ".crewplane/execution-stages"
            / receipt.extraction_run_key
            / "manifests/run.json"
        ).read_bytes()
    )
    assert child_manifest["status"] == "cancelled"
    assert (parent.stages_dir / "manifests/run.json").read_bytes() == original
    assert not list((tmp_path / ".crewplane/locks").iterdir())


def test_import_failure_does_not_regenerate_a_successful_result(tmp_path: Path):
    config, parent, packet, fixtures, console = setup_parent(tmp_path)
    document = lesson_document(
        LessonProposal(
            title="Oversized",
            body="x" * (33 * 1024),
            evidence_ids=(packet.evidence[0].evidence_id,),
        )
    )
    (fixtures / "extract.md").write_text(
        "```crewplane-memory-candidates\n" + document.model_dump_json() + "\n```",
        encoding="utf-8",
        newline="\n",
    )
    result = asyncio.run(extract_memory(config, parent, console))
    assert "import failed" in result.message
    receipt = read_extraction(parent.stages_dir)
    assert receipt.provider_status == receipt.validation_status == "succeeded"
    assert receipt.import_status == "failed"
    assert not asyncio.run(MemoryStore(tmp_path).snapshot()).records
    repeated = asyncio.run(extract_memory(config, parent, console))
    assert "import failed" in repeated.message
    assert len(list((tmp_path / ".crewplane/execution-stages").iterdir())) == 2
