from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Literal


def update_workspace_ref_publication(
    state_path: Path, publication: Mapping[str, object]
) -> None:
    def apply(payload: dict[str, object]) -> None:
        existing = payload.get("ref_publication")
        if existing is not None and existing != dict(publication):
            raise RuntimeError("Workspace ref publication evidence is immutable.")
        payload["ref_publication"] = deepcopy(dict(publication))

    _mutate(state_path, apply)


def update_workspace_ref_publication_phase(state_path: Path, phase: str) -> None:
    def apply(payload: dict[str, object]) -> None:
        publication = payload.get("ref_publication")
        if not isinstance(publication, dict):
            raise RuntimeError("Workspace state lacks ref publication evidence.")
        current = publication.get("phase")
        allowed = {
            "prepared": {"published", "removed"},
            "published": {"removed"},
            "removed": set(),
        }
        if phase != current and phase not in allowed.get(str(current), set()):
            raise RuntimeError(
                f"Invalid ref publication phase transition: {current!r} -> {phase!r}."
            )
        publication["phase"] = phase

    _mutate(state_path, apply)


def record_workspace_process_drain(
    state_path: Path,
    status: Literal["confirmed", "unresolved"],
    pid: int,
    process_group_id: int | None,
    reason: str | None = None,
) -> None:
    def apply(payload: dict[str, object]) -> None:
        evidence: dict[str, object] = {
            "status": status,
            "pid": pid,
            "process_group_id": process_group_id,
        }
        if reason is not None:
            evidence["reason"] = reason
        payload["process_drain"] = evidence

    _mutate(state_path, apply)


def record_workspace_temporary_ref(
    state_path: Path, ref_name: str, target_oid: str
) -> None:
    def apply(payload: dict[str, object]) -> None:
        claims = payload.setdefault("temporary_refs", [])
        if not isinstance(claims, list):
            raise RuntimeError("Workspace temporary ref evidence is invalid.")
        existing = [
            claim
            for claim in claims
            if isinstance(claim, dict) and claim.get("name") == ref_name
        ]
        if existing:
            if any(claim.get("target_oid") != target_oid for claim in existing):
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

    _mutate(state_path, apply)


def mark_workspace_temporary_ref_removed(state_path: Path, ref_name: str) -> None:
    def apply(payload: dict[str, object]) -> None:
        claims = payload.get("temporary_refs")
        if not isinstance(claims, list):
            raise RuntimeError("Workspace temporary ref evidence is missing.")
        matching = [
            claim
            for claim in claims
            if isinstance(claim, dict) and claim.get("name") == ref_name
        ]
        if len(matching) != 1:
            raise RuntimeError("Workspace temporary ref evidence is ambiguous.")
        matching[0]["phase"] = "removed"

    _mutate(state_path, apply)


def update_workspace_setup(
    state_path: Path,
    setup: Mapping[str, object],
    base_payload: Mapping[str, object] | None = None,
) -> None:
    def apply(payload: dict[str, object]) -> None:
        if base_payload is not None:
            from .state import require_workspace_state_payload_identity

            require_workspace_state_payload_identity(payload, base_payload)
        payload["setup"] = deepcopy(dict(setup))

    _mutate(state_path, apply)


def _mutate(state_path: Path, mutation: Callable[[dict[str, object]], None]) -> None:
    from .state import mutate_workspace_state

    mutate_workspace_state(state_path, mutation)
