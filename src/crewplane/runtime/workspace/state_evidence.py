from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, TypeIs

from .mutator_fence import fence_workspace_mutator, release_workspace_mutator
from .state import edit_workspace_state, require_workspace_state_payload_identity

if TYPE_CHECKING:
    from crewplane.runtime.agent.process.drain import ProcessDrainError


type _RefPublicationPhase = Literal["prepared", "published", "removed"]
type _RefPublicationTargetPhase = Literal["published", "removed"]
type _TemporaryRefClaim = dict[str, object]

_REF_PUBLICATION_TRANSITIONS: Final[
    dict[_RefPublicationPhase, frozenset[_RefPublicationTargetPhase]]
] = {
    "prepared": frozenset({"published", "removed"}),
    "published": frozenset({"removed"}),
    "removed": frozenset(),
}


def update_workspace_ref_publication(
    state_path: Path, publication: Mapping[str, object]
) -> None:
    publication_payload = deepcopy(dict(publication))
    with edit_workspace_state(state_path) as payload:
        existing = payload.get("ref_publication")
        if existing is not None and existing != publication_payload:
            raise RuntimeError("Workspace ref publication evidence is immutable.")
        payload["ref_publication"] = publication_payload


def update_workspace_ref_publication_phase(
    state_path: Path,
    phase: _RefPublicationTargetPhase,
) -> None:
    with edit_workspace_state(state_path) as payload:
        publication = payload.get("ref_publication")
        if not isinstance(publication, dict):
            raise RuntimeError("Workspace state lacks ref publication evidence.")
        current = publication.get("phase")
        if not _is_ref_publication_phase(current) or phase not in (
            "published",
            "removed",
        ):
            raise RuntimeError(
                f"Invalid ref publication phase transition: {current!r} -> {phase!r}."
            )
        if phase != current and phase not in _REF_PUBLICATION_TRANSITIONS[current]:
            raise RuntimeError(
                f"Invalid ref publication phase transition: {current!r} -> {phase!r}."
            )
        publication["phase"] = phase


def record_workspace_process_drain(
    state_path: Path,
    status: Literal["confirmed", "unresolved"],
    pid: int,
    process_group_id: int | None,
    reason: str | None = None,
) -> None:
    with edit_workspace_state(state_path) as payload:
        evidence: dict[str, object] = {
            "status": status,
            "pid": pid,
            "process_group_id": process_group_id,
        }
        if reason is not None:
            evidence["reason"] = reason
        payload["process_drain"] = evidence


def confirm_workspace_process_drain(
    state_path: Path | None,
    pid: int,
    process_group_id: int | None,
) -> None:
    if state_path is None:
        return
    record_workspace_process_drain(state_path, "confirmed", pid, process_group_id)
    release_workspace_mutator(state_path)


def record_unresolved_workspace_process_drain(
    state_path: Path | None,
    error: ProcessDrainError,
) -> None:
    if state_path is None:
        return
    fence_workspace_mutator(state_path)
    try:
        record_workspace_process_drain(
            state_path,
            "unresolved",
            error.evidence.pid,
            error.evidence.process_group_id,
            str(error),
        )
    except Exception as persistence_error:
        error.add_note(
            f"Workspace process-drain evidence persistence failed: {persistence_error}"
        )


def record_workspace_temporary_ref(
    state_path: Path, ref_name: str, target_oid: str
) -> None:
    with edit_workspace_state(state_path) as payload:
        claims = payload.setdefault("temporary_refs", [])
        if not _is_temporary_ref_claim_list(claims):
            raise RuntimeError("Workspace temporary ref evidence is invalid.")
        matching = _matching_temporary_ref_claims(claims, ref_name)
        if len(matching) > 1:
            raise RuntimeError("Workspace temporary ref evidence is ambiguous.")
        if matching:
            if matching[0].get("target_oid") != target_oid:
                raise RuntimeError("Workspace temporary ref evidence conflicts.")
            return
        git_payload = payload.get("git")
        repository_id = (
            git_payload.get("repo_id") if isinstance(git_payload, dict) else None
        )
        claims.append(
            {
                "phase": "prepared",
                "name": ref_name,
                "target_oid": target_oid,
                "owner_run_id": payload.get("run_id"),
                "owner_node_id": payload.get("node_id"),
                "owner_task_id": payload.get("task_id"),
                "owner_role": payload.get("role"),
                "owner_round_num": payload.get("round_num"),
                "owner_audit_round_num": payload.get("audit_round_num"),
                "repository_id": repository_id,
            }
        )


def mark_workspace_temporary_ref_removed(state_path: Path, ref_name: str) -> None:
    with edit_workspace_state(state_path) as payload:
        claims = payload.get("temporary_refs")
        if claims is None:
            raise RuntimeError("Workspace temporary ref evidence is missing.")
        if not _is_temporary_ref_claim_list(claims):
            raise RuntimeError("Workspace temporary ref evidence is invalid.")
        matching = _matching_temporary_ref_claims(claims, ref_name)
        if len(matching) != 1:
            raise RuntimeError("Workspace temporary ref evidence is ambiguous.")
        matching[0]["phase"] = "removed"


def update_workspace_setup(
    state_path: Path,
    setup: Mapping[str, object],
    base_payload: Mapping[str, object],
) -> None:
    with edit_workspace_state(state_path) as payload:
        require_workspace_state_payload_identity(payload, base_payload)
        payload["setup"] = deepcopy(dict(setup))


def _is_ref_publication_phase(value: object) -> TypeIs[_RefPublicationPhase]:
    return isinstance(value, str) and value in {
        "prepared",
        "published",
        "removed",
    }


def _is_temporary_ref_claim_list(
    value: object,
) -> TypeIs[list[_TemporaryRefClaim]]:
    return isinstance(value, list) and all(
        _is_temporary_ref_claim(claim) for claim in value
    )


def _is_temporary_ref_claim(value: object) -> TypeIs[_TemporaryRefClaim]:
    return (
        isinstance(value, dict)
        and value.get("phase") in {"prepared", "removed"}
        and isinstance(value.get("name"), str)
        and bool(value["name"])
        and isinstance(value.get("target_oid"), str)
        and bool(value["target_oid"])
    )


def _matching_temporary_ref_claims(
    claims: list[_TemporaryRefClaim],
    ref_name: str,
) -> list[_TemporaryRefClaim]:
    return [claim for claim in claims if claim["name"] == ref_name]
