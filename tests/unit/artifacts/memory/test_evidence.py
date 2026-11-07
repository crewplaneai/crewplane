from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console

from crewplane.artifacts.atomic import atomic_write_json, json_bytes
from crewplane.artifacts.expansions import reconstruct_expansions
from crewplane.artifacts.memory.evidence import (
    NoEligibleEvidence,
    capture_evidence,
    pack_evidence,
)
from crewplane.artifacts.memory.outcomes import (
    node_invocation_qualifiers,
    read_invocations,
    read_process_evidence,
    review_qualifiers,
)
from crewplane.artifacts.memory.receipts import PACKET_LIMIT_BYTES, read_packet
from crewplane.artifacts.naming import node_state_relative_path
from crewplane.cli.run.execution import execute_workflow_run
from crewplane.core.config import MemorySettings
from crewplane.core.execution_state import ResumeOrigin, ReviewCheckpointResumeSummary
from crewplane.core.memory.models import digest_payload
from crewplane.core.memory.packets import EvidencePacket
from crewplane.core.preflight.source import load_workflow_source_for_preflight
from tests.helpers.foreach import (
    copy_foreach_fixture,
    workflow_document,
    write_workflow_document,
)
from tests.helpers.memory import evidence_receipt, initialize_memory
from tests.helpers.memory_runtime import setup_parent
from tests.helpers.mock_resume import write_executor_fixture
from tests.integration.cli.workflow_runner_support import mock_runner_config
from tests.unit.artifacts.review_loop_status_support import (
    create_referenced_outputs,
    valid_status_payload,
    write_status,
)


def clear_packet(parent):
    (parent.stages_dir / "manifests/memory/evidence.json").unlink()


def test_verified_sources_tamper_rejection_and_saved_packet_reuse(tmp_path: Path):
    _config, parent, packet, _fixtures, _console = setup_parent(tmp_path)
    source = Path(packet.evidence[0].source_locator)
    source.write_bytes(b"tampered")
    assert (
        capture_evidence(parent.plan, parent.stages_dir, parent.results_dir) == packet
    )
    clear_packet(parent)
    with pytest.raises(NoEligibleEvidence, match="No new usable"):
        capture_evidence(parent.plan, parent.stages_dir, parent.results_dir)


@pytest.mark.parametrize("corruption", ["expansion", "findings"])
def test_generated_evidence_isolates_invalid_retained_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corruption: str
):
    root = tmp_path / "project"
    workflow = copy_foreach_fixture(root)
    child = root / "analyze.md"
    payload, body = workflow_document(child)
    payload["memory"] = True
    write_workflow_document(child, payload, body)
    initialize_memory(root)
    monkeypatch.chdir(root)
    fixtures = tmp_path / "fixtures"
    write_executor_fixture(
        fixtures,
        "planner",
        '<!-- findings -->[{"chunk":"a"},{"chunk":"b"}]<!-- /findings -->',
    )
    write_executor_fixture(fixtures, "summarize", "Summary")
    node_ids = tuple(f"chunks.i{ordinal}.part.analyze" for ordinal in range(2))
    for node_id in node_ids:
        write_executor_fixture(
            fixtures, node_id, "Analysis\n<!-- findings -->done<!-- /findings -->"
        )
    config = mock_runner_config(
        {"output_mode": "file", "output_dir": str(fixtures), "strict_file_mode": True}
    )
    config.settings.memory = MemorySettings(learn=False)
    config.settings.max_concurrent_nodes = 1
    parent = asyncio.run(
        execute_workflow_run(
            config,
            load_workflow_source_for_preflight(workflow, root),
            False,
            True,
            Console(),
            project_root=root,
        )
    )
    assert parent.can_learn
    assert parent.plan is not None
    assert parent.stages_dir is not None and parent.results_dir is not None
    packet = capture_evidence(parent.plan, parent.stages_dir, parent.results_dir)
    assert [receipt.node_id for receipt in packet.evidence] == [
        node_ids[0],
        node_ids[0],
        node_ids[1],
        node_ids[1],
    ]
    clear_packet(parent)
    expanded = reconstruct_expansions(parent.plan, parent.stages_dir)
    node = next(node for node in expanded.plan.nodes if node.id == node_ids[0])
    assert node.expansion is not None
    state_path = parent.stages_dir / node_state_relative_path(node.id)
    state = json.loads(state_path.read_bytes())
    assert state["expansion"] == node.expansion.model_dump(mode="json")
    expected_qualifications = packet.qualifications
    if corruption == "expansion":
        state["expansion"]["digest"] = "0" * 64
        atomic_write_json(state_path, state)
        expected_receipts = packet.evidence[2:]
        expected_qualifications = tuple(
            value
            for value in expected_qualifications
            if not value.startswith(f"{node.id}: ")
        )
        expected_error = "node state identity mismatch"
    else:
        Path(packet.evidence[1].source_locator).unlink()
        expected_receipts = (packet.evidence[0], *packet.evidence[2:])
        expected_error = "findings artifact is unavailable"

    packet = capture_evidence(parent.plan, parent.stages_dir, parent.results_dir)

    assert [
        receipt.model_dump(exclude={"captured_at"}) for receipt in packet.evidence
    ] == [receipt.model_dump(exclude={"captured_at"}) for receipt in expected_receipts]
    assert packet.qualifications == expected_qualifications
    assert len(packet.omissions) == 1
    assert node_ids[0] in packet.omissions[0]
    assert expected_error in packet.omissions[0]
    assert read_packet(parent.stages_dir) == packet


def test_unexpected_artifact_failure_propagates_without_saving(tmp_path: Path):
    _config, parent, _packet, _fixtures, _console = setup_parent(tmp_path)
    clear_packet(parent)

    with (
        patch(
            "crewplane.artifacts.memory.evidence.read_verified_node_artifact",
            side_effect=RuntimeError("artifact verifier failed"),
        ),
        pytest.raises(RuntimeError, match="artifact verifier failed"),
    ):
        capture_evidence(parent.plan, parent.stages_dir, parent.results_dir)

    assert not (parent.stages_dir / "manifests/memory/evidence.json").exists()


@pytest.mark.parametrize("field", ["run_id", "run_key_name", "workflow_signature"])
def test_saved_packet_rejects_mismatched_plan_without_overwriting(
    tmp_path: Path, field: str
):
    _config, parent, _packet, _fixtures, _console = setup_parent(tmp_path)
    packet_path = parent.stages_dir / "manifests/memory/evidence.json"
    saved = packet_path.read_bytes()
    plan = parent.plan.model_copy(update={field: "other"})

    with pytest.raises(ValueError, match="Saved memory packet does not belong"):
        capture_evidence(plan, parent.stages_dir, parent.results_dir)

    assert packet_path.read_bytes() == saved


@pytest.mark.parametrize(
    ("field", "value"),
    [("run_id", "other"), ("workflow_signature", "0" * 64), ("status", "cancelled")],
)
def test_capture_rejects_ineligible_manifest_without_saving(
    tmp_path: Path, field: str, value: str
):
    _config, parent, _packet, _fixtures, _console = setup_parent(tmp_path)
    clear_packet(parent)
    manifest_path = parent.stages_dir / "manifests/run.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest[field] = value
    if field == "status":
        manifest["cancel_reason"] = "cancelled before learning"
    atomic_write_json(manifest_path, manifest)

    with pytest.raises(NoEligibleEvidence, match="matching completed, non-cancelled"):
        capture_evidence(parent.plan, parent.stages_dir, parent.results_dir)

    assert not (parent.stages_dir / "manifests/memory/evidence.json").exists()


@pytest.mark.parametrize("length", [4096, 4097])
def test_receipt_preserves_unicode_excerpt_bounds_and_workspace_provenance(
    tmp_path: Path, length: int
):
    _config, parent, original, _fixtures, _console = setup_parent(tmp_path)
    clear_packet(parent)
    node = parent.plan.nodes[0]
    state_path = parent.stages_dir / node_state_relative_path(node.id)
    state = json.loads(state_path.read_bytes())
    payload = ("é" * length).encode("utf-8")
    source = Path(original.evidence[0].source_locator)
    source.write_bytes(payload)
    descriptor = next(item for item in state["artifacts"] if item["kind"] == "output")
    descriptor.update(
        size_bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest()
    )
    state["workspace"] = {"z": "last", "a": "first"}
    atomic_write_json(state_path, state)

    packet = capture_evidence(parent.plan, parent.stages_dir, parent.results_dir)

    assert len(packet.evidence) == 1
    receipt = packet.evidence[0]
    assert receipt.excerpt == "é" * 4096
    assert (
        receipt.excerpt_sha256 == hashlib.sha256(receipt.excerpt.encode()).hexdigest()
    )
    assert receipt.source_sha256 == descriptor["sha256"]
    assert receipt.source_locator == source.as_posix()
    assert (
        receipt.evidence_id
        == "e-"
        + digest_payload(
            {"node": node.id, "kind": "output", "source": descriptor["sha256"]}
        )[:24]
    )
    assert receipt.observed_at == state["completed_at"]
    assert receipt.captured_at == packet.captured_at
    expected_limits = original.evidence[0].qualifications
    if length > 4096:
        expected_limits += (
            "Excerpt includes only the first 4096 characters; original source SHA256 covers the complete retained artifact.",
        )
    assert receipt.qualifications == expected_limits
    assert receipt.provenance == (
        *original.evidence[0].provenance,
        'Retained workspace provenance: {"a": "first", "z": "last"}',
    )
    assert read_packet(parent.stages_dir) == packet


def test_missing_invocation_evidence_is_unknown_not_success_proof(tmp_path: Path):
    _config, parent, _packet, _fixtures, _console = setup_parent(
        tmp_path, log_output=False
    )
    clear_packet(parent)
    (parent.stages_dir / "logs/events.ndjson").unlink()
    packet = capture_evidence(parent.plan, parent.stages_dir, parent.results_dir)
    assert any("outcomes are unknown" in value for value in packet.qualifications)
    assert all(receipt.kind == "agent_output" for receipt in packet.evidence)
    assert "Agent reports tests passed" not in " ".join(packet.qualifications)


@pytest.mark.parametrize("new_invocations", [False, True])
def test_checkpoint_reuse_distinguishes_mixed_new_work(
    tmp_path: Path, new_invocations: bool
):
    _config, parent, _packet, _fixtures, _console = setup_parent(tmp_path)
    clear_packet(parent)
    node = parent.plan.nodes[0]
    origin = ResumeOrigin(
        source_run_id="archived",
        source_run_key_name="archived-run",
        source_node_id=node.id,
        hydrated_at="2026-01-01T00:00:00Z",
    )
    state_path = parent.stages_dir / node_state_relative_path(node.id)
    state = json.loads(state_path.read_bytes())
    state["resume_origin"] = origin.model_dump(mode="json")
    atomic_write_json(state_path, state)
    manifest_path = parent.stages_dir / "manifests/run.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["resume_source_run_id"] = origin.source_run_id
    manifest["resume_source_run_key_name"] = origin.source_run_key_name
    manifest["resumed_review_checkpoints"] = [
        ReviewCheckpointResumeSummary(
            node_id=node.id,
            source_audit=1,
            source_local_round=1,
            source_phase="reviewers",
            resume_origin=origin,
        ).model_dump(mode="json")
    ]
    atomic_write_json(manifest_path, manifest)
    if new_invocations:
        packet = capture_evidence(parent.plan, parent.stages_dir, parent.results_dir)
        assert any(
            "Checkpoint reuse plus new invocations" in value
            for value in packet.evidence[0].provenance
        )
        assert any("archived-run" in value for value in packet.evidence[0].provenance)
    else:
        events = parent.stages_dir / "logs/events.ndjson"
        records = [json.loads(line) for line in events.read_text().splitlines()]
        events.write_text(
            "\n".join(
                json.dumps(record)
                for record in records
                if not record["event_type"].startswith("invocation_")
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        with pytest.raises(NoEligibleEvidence, match="entirely reused"):
            capture_evidence(parent.plan, parent.stages_dir, parent.results_dir)


def test_complete_unit_packing_and_mandatory_context_overflow(tmp_path: Path):
    _config, parent, _packet, _fixtures, _console = setup_parent(tmp_path)
    evidence = tuple(
        evidence_receipt("x" * 4096, parent.plan.run_id).model_copy(
            update={"evidence_id": f"e-{index}"}
        )
        for index in range(20)
    )
    packet = pack_evidence(
        parent.plan,
        "succeeded",
        "2026-01-01T00:00:00Z",
        ("Not verification.",),
        evidence,
        (),
    )
    assert 0 < len(packet.evidence) < len(evidence)
    assert all(len(item.excerpt) == 4096 for item in packet.evidence)
    assert packet.omissions
    assert len(json_bytes(packet.model_dump(mode="json"))) <= PACKET_LIMIT_BYTES
    with pytest.raises(NoEligibleEvidence, match="Mandatory outcome"):
        pack_evidence(
            parent.plan,
            "failed",
            "2026-01-01T00:00:00Z",
            ("x" * PACKET_LIMIT_BYTES,),
            evidence,
            (),
        )
    changed = packet.model_dump(mode="json")
    changed["parent_outcome"] = "failed"
    with pytest.raises(ValueError, match="identity"):
        EvidencePacket.model_validate(changed)


@pytest.mark.parametrize(
    "corruption", ["malformed", "wrong-run", "unknown-event", "wrong-role", "oversized"]
)
def test_structured_events_are_bounded_and_never_best_effort_skipped(
    tmp_path: Path, corruption
):
    logs = tmp_path / "logs"
    logs.mkdir()
    event = {
        "event_type": "invocation_failed",
        "node_id": "node",
        "run_id": "run",
        "timestamp": "2026-01-01T00:00:00Z",
        "task_id": "task",
        "provider": "alpha",
        "role": "executor",
        "round_num": 1,
    }
    if corruption == "wrong-run":
        event["run_id"] = "other"
    elif corruption == "unknown-event":
        event["event_type"] = "unrecognized"
    elif corruption == "wrong-role":
        event["role"] = "system"
    payload = json.dumps(event).encode()
    if corruption == "malformed":
        payload += b"\nnot JSON"
    if corruption == "oversized":
        payload = b" " * (8 * 1024 * 1024 + 1)
    (logs / "events.ndjson").write_bytes(payload)
    evidence = read_invocations(tmp_path, "run")
    assert evidence.limitation is not None
    assert not evidence.events
    assert "unknown" in node_invocation_qualifiers("node", evidence)[0]


def test_partial_invocation_failure_is_preserved_despite_completed_node(tmp_path: Path):
    _config, parent, _packet, _fixtures, _console = setup_parent(tmp_path)
    clear_packet(parent)
    events = parent.stages_dir / "logs/events.ndjson"
    records = [json.loads(line) for line in events.read_text().splitlines()]
    failed = next(
        record.copy()
        for record in records
        if record["event_type"] == "invocation_finished"
    )
    failed.update(
        event_type="invocation_failed",
        task_id="second_provider",
        error="partial failure",
    )
    records.append(failed)
    events.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    packet = capture_evidence(parent.plan, parent.stages_dir, parent.results_dir)
    assert any("1 failed" in value for value in packet.qualifications)
    assert any("second_provider" in value for value in packet.evidence[0].provenance)


def test_review_status_retains_unresolved_consensus_and_selected_hashes(tmp_path: Path):
    _config, parent, _packet, _fixtures, _console = setup_parent(tmp_path)
    node = parent.plan.nodes[0]
    provider = node.provider_records[0]
    node = node.model_copy(
        update={
            "id": "stage",
            "provider_records": [
                provider.model_copy(
                    update={
                        "task_id": "executor",
                        "provider": "codex",
                        "role": "executor",
                    }
                ),
                provider.model_copy(
                    update={
                        "task_id": "reviewer",
                        "provider": "claude",
                        "role": "reviewer",
                    }
                ),
            ],
        }
    )
    stage = tmp_path / "stage"
    stage.mkdir()
    create_referenced_outputs(stage)
    payload = valid_status_payload()
    payload.update(consensus_reached=False, continued_after_consensus_exhaustion=True)
    write_status(stage, payload)
    qualifiers = review_qualifiers(tmp_path, node)
    assert "consensus reached: False" in qualifiers[0]
    assert "exhaustion: True" in qualifiers[0]
    assert any("SHA256" in value for value in qualifiers)
    (stage / "review-state/review-loop-status.json").write_bytes(b"invalid")
    assert "unknown" in review_qualifiers(tmp_path, node)[0]


def test_process_receipt_strength_and_missing_provenance(tmp_path: Path):
    assert (
        "unavailable"
        in read_process_evidence(tmp_path, "run").qualifications("node")[0]
    )
    root = tmp_path / "manifests/provider-processes"
    root.mkdir(parents=True)
    payload = {
        "run_state_schema_version": 1,
        "run_id": "run",
        "run_key_name": "run-key",
        "node_id": "node",
        "task_id": "alpha",
        "provider": "alpha",
        "role": "executor",
        "round_num": 1,
        "attempt": 1,
        "pid": 100,
        "hostname": "test",
        "status": "exited",
        "started_at": "2026-01-01T00:00:00Z",
        "exited_at": "2026-01-01T00:01:00Z",
        "returncode": 0,
    }
    atomic_write_json(root / "process.json", payload)
    qualifiers = read_process_evidence(tmp_path, "run").qualifications("node")
    assert "return code 0" in qualifiers[0]
    assert "not proof that tests passed" in qualifiers[0]
    assert "SHA256" in qualifiers[0]
    (root / "broken.json").write_bytes(b"broken")
    assert "unknown" in read_process_evidence(tmp_path, "run").qualifications("node")[0]


def test_process_inventory_stops_at_the_documented_capture_bound(tmp_path: Path):
    root = tmp_path / "manifests/provider-processes"
    root.mkdir(parents=True)
    for index in range(1025):
        (root / f"{index}.json").write_bytes(b"not read once inventory exceeds limit")
    evidence = read_process_evidence(tmp_path, "run")
    assert not evidence.facts
    assert "inventory exceeds capture bound" in evidence.qualifications("node")[0]
