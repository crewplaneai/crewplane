from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest

import crewplane.runtime.workspace.state_evidence as evidence
from crewplane.runtime.agent.process.drain import (
    ProcessDrainError,
    ProcessDrainEvidence,
)
from crewplane.runtime.workspace.mutator_fence import (
    fence_workspace_mutator,
    release_workspace_mutator,
    workspace_mutator_is_fenced,
)
from crewplane.runtime.workspace.terminalization import workspace_mutators_are_drained


def _write_payload(state_path: Path, payload: dict[str, object]) -> None:
    state_path.write_text(json.dumps(payload), encoding="utf-8")


def _read_payload(state_path: Path) -> dict[str, object]:
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


@pytest.mark.parametrize("confirmed", [False, True])
@pytest.mark.parametrize("write_fails", [False, True])
def test_process_drain_transition_orders_evidence_and_cleanup_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, confirmed: bool, write_fails: bool
) -> None:
    state_path = tmp_path / "workspace-state.json"
    _write_payload(state_path, {"process_drain": {"status": "not_started"}})
    error = ProcessDrainError(
        ProcessDrainEvidence(12, 13, True, False), "process group remains live"
    )
    persistence_error = OSError("state write failed")
    original_record = evidence.record_workspace_process_drain
    writes: list[str] = []
    if confirmed:
        fence_workspace_mutator(state_path)

    def record_while_fenced(
        path: Path,
        status: Literal["confirmed", "unresolved"],
        pid: int,
        process_group_id: int | None,
        reason: str | None = None,
    ) -> None:
        assert path == state_path
        assert workspace_mutator_is_fenced(state_path)
        assert not workspace_mutators_are_drained(state_path)
        writes.append(status)
        if write_fails:
            raise persistence_error
        original_record(path, status, pid, process_group_id, reason)

    monkeypatch.setattr(evidence, "record_workspace_process_drain", record_while_fenced)
    try:
        if confirmed and write_fails:
            with pytest.raises(OSError) as caught:
                evidence.confirm_workspace_process_drain(state_path, 12, 13)
            assert caught.value is persistence_error
        elif confirmed:
            evidence.confirm_workspace_process_drain(state_path, 12, 13)
        else:
            evidence.record_unresolved_workspace_process_drain(state_path, error)
            assert getattr(error, "__notes__", []) == (
                [
                    "Workspace process-drain evidence persistence failed: state write failed"
                ]
                if write_fails
                else []
            )

        assert writes == ["confirmed" if confirmed else "unresolved"]
        assert workspace_mutators_are_drained(state_path) is (
            confirmed and not write_fails
        )
        assert workspace_mutator_is_fenced(state_path) is (not confirmed or write_fails)
        payload = _read_payload(state_path)
        assert payload["process_drain"]["status"] == (
            "not_started" if write_fails else "confirmed" if confirmed else "unresolved"
        )
    finally:
        release_workspace_mutator(state_path)


def test_process_drain_transitions_allow_absent_workspace_state() -> None:
    error = ProcessDrainError(ProcessDrainEvidence(12, None, True, False), "live")

    evidence.confirm_workspace_process_drain(None, 12, None)
    evidence.record_unresolved_workspace_process_drain(None, error)

    assert not getattr(error, "__notes__", [])


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
    _write_payload(state_path, current_payload)

    evidence.update_workspace_setup(
        state_path,
        {"status": "succeeded"},
        base_payload=trusted_payload,
    )

    updated_payload = _read_payload(state_path)
    assert updated_payload["setup"] == {"status": "succeeded"}
    assert updated_payload["process_drain"] == current_payload["process_drain"]

    updated_payload["run_id"] = "setup-mutated-run"
    _write_payload(state_path, updated_payload)

    with pytest.raises(RuntimeError, match="state identity changed"):
        evidence.update_workspace_setup(
            state_path,
            {"status": "failed"},
            base_payload=trusted_payload,
        )

    persisted_payload = _read_payload(state_path)
    assert persisted_payload["setup"] == {"status": "succeeded"}


def test_operational_workspace_evidence_mutations(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "workspace-state.json"
    payload: dict[str, object] = {
        "run_id": "run",
        "node_id": "node",
        "task_id": "task",
        "role": "executor",
        "round_num": 1,
        "audit_round_num": None,
        "git": {"repo_id": "repo"},
    }
    _write_payload(state_path, payload)

    evidence.record_workspace_process_drain(
        state_path, "unresolved", 12, 13, "still alive"
    )
    evidence.record_workspace_temporary_ref(state_path, "refs/test", "a" * 40)
    evidence.record_workspace_temporary_ref(state_path, "refs/test", "a" * 40)
    evidence.mark_workspace_temporary_ref_removed(state_path, "refs/test")
    evidence.update_workspace_setup(
        state_path,
        {"status": "succeeded"},
        base_payload=_read_payload(state_path),
    )
    publication = {
        "phase": "prepared",
        "destinations": {},
    }
    evidence.update_workspace_ref_publication(state_path, publication)
    evidence.update_workspace_ref_publication_phase(state_path, "published")
    evidence.update_workspace_ref_publication_phase(state_path, "published")
    evidence.update_workspace_ref_publication_phase(state_path, "removed")

    payload = _read_payload(state_path)
    assert payload["process_drain"] == {
        "status": "unresolved",
        "pid": 12,
        "process_group_id": 13,
        "reason": "still alive",
    }
    assert payload["setup"] == {"status": "succeeded"}
    assert payload["ref_publication"]["phase"] == "removed"


def test_operational_workspace_evidence_rejects_contradictions(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "workspace-state.json"
    payload: dict[str, object] = {}
    _write_payload(state_path, payload)

    with pytest.raises(RuntimeError, match="lacks ref publication evidence"):
        evidence.update_workspace_ref_publication_phase(state_path, "published")
    payload["temporary_refs"] = "invalid"
    _write_payload(state_path, payload)
    with pytest.raises(RuntimeError, match="temporary ref evidence is invalid"):
        evidence.record_workspace_temporary_ref(state_path, "refs/test", "a" * 40)
    payload.pop("temporary_refs")
    _write_payload(state_path, payload)
    with pytest.raises(RuntimeError, match="missing"):
        evidence.mark_workspace_temporary_ref_removed(state_path, "refs/test")
    payload["temporary_refs"] = [
        {"phase": "prepared", "name": "refs/test", "target_oid": "a" * 40}
    ]
    _write_payload(state_path, payload)
    with pytest.raises(RuntimeError, match="conflicts"):
        evidence.record_workspace_temporary_ref(state_path, "refs/test", "b" * 40)
    payload["ref_publication"] = {"phase": "removed"}
    _write_payload(state_path, payload)
    with pytest.raises(RuntimeError, match="Invalid ref publication phase"):
        evidence.update_workspace_ref_publication_phase(state_path, "published")
    with pytest.raises(RuntimeError, match="immutable"):
        evidence.update_workspace_ref_publication(
            state_path, {"phase": "prepared", "destinations": {}}
        )


def test_ref_publication_phase_rejects_unknown_idempotent_transition(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "workspace-state.json"
    payload: dict[str, object] = {"ref_publication": {"phase": "corrupt"}}
    _write_payload(state_path, payload)

    with pytest.raises(RuntimeError, match="Invalid ref publication phase"):
        evidence.update_workspace_ref_publication_phase(state_path, "corrupt")  # type: ignore[arg-type]

    assert _read_payload(state_path) == payload


def test_record_workspace_temporary_ref_rejects_malformed_claims(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "workspace-state.json"
    payload: dict[str, object] = {"temporary_refs": [42]}
    _write_payload(state_path, payload)

    with pytest.raises(RuntimeError, match="temporary ref evidence is invalid"):
        evidence.record_workspace_temporary_ref(state_path, "refs/test", "a" * 40)

    assert _read_payload(state_path) == payload


def test_record_workspace_temporary_ref_rejects_duplicate_claims(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "workspace-state.json"
    claim = {"phase": "prepared", "name": "refs/test", "target_oid": "a" * 40}
    payload: dict[str, object] = {"temporary_refs": [claim, claim.copy()]}
    _write_payload(state_path, payload)

    with pytest.raises(RuntimeError, match="temporary ref evidence is ambiguous"):
        evidence.record_workspace_temporary_ref(state_path, "refs/test", "a" * 40)

    assert _read_payload(state_path) == payload
