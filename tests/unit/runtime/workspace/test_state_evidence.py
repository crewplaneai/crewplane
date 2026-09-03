from __future__ import annotations

import json
from pathlib import Path

import pytest

import crewplane.runtime.workspace.state_evidence as evidence


def test_workspace_setup_update_validates_identity_and_preserves_runtime_evidence(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "workspace-state.json"
    trusted_payload: dict[str, object] = {
        "run_id": "run-001",
        "process_drain": {"status": "not_started"},
        "setup": {"status": "pending"},
    }
    current_payload = {
        **trusted_payload,
        "process_drain": {
            "status": "confirmed",
            "pid": 123,
            "process_group_id": 123,
        },
    }
    state_path.write_text(json.dumps(current_payload), encoding="utf-8")

    evidence.update_workspace_setup(
        state_path,
        {"status": "succeeded"},
        base_payload=trusted_payload,
    )

    updated_payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert updated_payload["setup"] == {"status": "succeeded"}
    assert updated_payload["process_drain"] == current_payload["process_drain"]

    updated_payload["run_id"] = "setup-mutated-run"
    state_path.write_text(json.dumps(updated_payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="state identity changed"):
        evidence.update_workspace_setup(
            state_path,
            {"status": "failed"},
            base_payload=trusted_payload,
        )

    persisted_payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted_payload["setup"] == {"status": "succeeded"}


def test_operational_workspace_evidence_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload: dict[str, object] = {
        "run_id": "run",
        "node_id": "node",
        "task_id": "task",
        "role": "executor",
        "round_num": 1,
        "audit_round_num": None,
        "git": {"repo_id": "repo"},
    }

    def mutate(path: Path, change) -> None:
        assert path == Path("workspace-state.json")
        change(payload)

    monkeypatch.setitem(vars(evidence), "_mutate", mutate)
    state_path = Path("workspace-state.json")

    evidence.record_workspace_process_drain(
        state_path, "unresolved", 12, 13, "still alive"
    )
    evidence.record_workspace_temporary_ref(state_path, "refs/test", "a" * 40)
    evidence.record_workspace_temporary_ref(state_path, "refs/test", "a" * 40)
    evidence.mark_workspace_temporary_ref_removed(state_path, "refs/test")
    evidence.update_workspace_setup(state_path, {"status": "succeeded"})
    publication = {
        "phase": "prepared",
        "destinations": {},
    }
    evidence.update_workspace_ref_publication(state_path, publication)
    evidence.update_workspace_ref_publication_phase(state_path, "published")
    evidence.update_workspace_ref_publication_phase(state_path, "published")
    evidence.update_workspace_ref_publication_phase(state_path, "removed")

    assert payload["process_drain"] == {
        "status": "unresolved",
        "pid": 12,
        "process_group_id": 13,
        "reason": "still alive",
    }
    assert payload["setup"] == {"status": "succeeded"}
    assert payload["ref_publication"]["phase"] == "removed"


def test_operational_workspace_evidence_rejects_contradictions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload: dict[str, object] = {}

    def mutate(path: Path, change) -> None:
        assert path == Path("workspace-state.json")
        change(payload)

    monkeypatch.setitem(vars(evidence), "_mutate", mutate)
    state_path = Path("workspace-state.json")

    with pytest.raises(RuntimeError, match="lacks ref publication evidence"):
        evidence.update_workspace_ref_publication_phase(state_path, "published")
    payload["temporary_refs"] = "invalid"
    with pytest.raises(RuntimeError, match="temporary ref evidence is invalid"):
        evidence.record_workspace_temporary_ref(state_path, "refs/test", "a" * 40)
    payload.pop("temporary_refs")
    with pytest.raises(RuntimeError, match="missing"):
        evidence.mark_workspace_temporary_ref_removed(state_path, "refs/test")
    payload["temporary_refs"] = [
        {"name": "refs/test", "target_oid": "a" * 40, "removed": False}
    ]
    with pytest.raises(RuntimeError, match="conflicts"):
        evidence.record_workspace_temporary_ref(state_path, "refs/test", "b" * 40)
    payload["ref_publication"] = {"phase": "removed"}
    with pytest.raises(RuntimeError, match="Invalid ref publication phase"):
        evidence.update_workspace_ref_publication_phase(state_path, "published")
    with pytest.raises(RuntimeError, match="immutable"):
        evidence.update_workspace_ref_publication(
            state_path, {"phase": "prepared", "destinations": {}}
        )
