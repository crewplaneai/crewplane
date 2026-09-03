from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeIs

from crewplane.artifacts.workspace.state.contracts import (
    require_workspace_state_contract,
)
from crewplane.core.workspace.invocation_identity import invocation_slug

from ..cleanup_notes import note_cleanup_failure
from ..git import GitCommand, git
from ..locks import git_metadata_lock
from ..state import (
    read_workspace_state,
    update_workspace_ref_publication,
    update_workspace_ref_publication_phase,
)
from .protected_refs import ProtectedRefSnapshot
from .refs import checked_ref, safe_ref_component
from .types import WorktreeCaptureRequest


@dataclass(frozen=True)
class RefPublicationDestination:
    name: str
    target_oid: str
    expected_old_oid: str | None


def publish_result_refs(
    request: WorktreeCaptureRequest,
    candidate_commit: str,
    result_commit: str,
    cancel_requested: Callable[[], bool] | None = None,
) -> tuple[str, str]:
    candidate_ref, result_ref = _result_ref_names(request)
    destinations = (
        RefPublicationDestination(candidate_ref, candidate_commit, None),
        RefPublicationDestination(result_ref, result_commit, None),
    )
    _persist_prepared_publication(request, destinations)
    with git_metadata_lock(Path(request.source.common_git_dir), cancel_requested):
        command = git(request.checkout_root)
        _publish_transaction(command, destinations, request.protected_refs)
    try:
        update_workspace_ref_publication_phase(request.state_path, "published")
    except Exception as exc:
        try:
            reconcile_result_ref_publication(
                request.state_path,
                Path(request.source.git_top_level),
                Path(request.source.common_git_dir),
                cancel_requested,
            )
        except Exception as cleanup_error:
            note_cleanup_failure(
                exc,
                "Workspace result ref cleanup after publication phase failure",
                cleanup_error,
            )
        raise
    return candidate_ref, result_ref


def reconcile_result_ref_publication(
    state_path: Path,
    repo_root: Path,
    common_git_dir: Path,
    cancel_requested: Callable[[], bool] | None = None,
) -> int:
    payload = read_workspace_state(state_path)
    _require_repository_identity(payload, repo_root, common_git_dir)
    destinations = publication_destinations(payload)
    if not destinations:
        return 0
    with git_metadata_lock(common_git_dir, cancel_requested):
        command = git(repo_root)
        _require_owned_publication_destinations(command, payload, destinations)
        matching = tuple(
            destination
            for destination in destinations
            if _ref_oid(command, destination.name) == destination.target_oid
        )
        mismatched = tuple(
            destination
            for destination in destinations
            if (current := _ref_oid(command, destination.name)) is not None
            and current != destination.target_oid
        )
        if matching:
            _delete_transaction(command, matching)
        remaining = tuple(
            destination
            for destination in destinations
            if _ref_oid(command, destination.name) is not None
        )
    if mismatched or remaining:
        names = ", ".join(destination.name for destination in (*mismatched, *remaining))
        raise RuntimeError(
            f"Workspace result refs moved or remain after exact-OID cleanup: {names}."
        )
    update_workspace_ref_publication_phase(state_path, "removed")
    return len(matching)


def _require_repository_identity(
    payload: dict[str, object], repo_root: Path, common_git_dir: Path
) -> None:
    git_evidence = payload.get("git")
    if not isinstance(git_evidence, dict):
        raise RuntimeError("Workspace ref publication lacks repository evidence.")
    git_top_level = git_evidence.get("git_top_level")
    recorded_common = git_evidence.get("common_git_dir")
    if not (
        isinstance(git_top_level, str)
        and Path(git_top_level).resolve(strict=False) == repo_root.resolve(strict=False)
        and isinstance(recorded_common, str)
        and Path(recorded_common).resolve(strict=False)
        == common_git_dir.resolve(strict=False)
    ):
        raise RuntimeError("Workspace ref publication repository identity changed.")


def publication_destinations(
    payload: dict[str, object],
) -> tuple[RefPublicationDestination, ...]:
    publication = payload.get("ref_publication")
    if not isinstance(publication, dict):
        return ()
    records = publication.get("destinations")
    if not isinstance(records, dict) or set(records) != {"candidate", "result"}:
        raise RuntimeError("Workspace ref publication destinations are incomplete.")
    return tuple(
        _publication_destination(records[label]) for label in ("candidate", "result")
    )


def _require_owned_publication_destinations(
    command: GitCommand,
    payload: dict[str, object],
    destinations: tuple[RefPublicationDestination, ...],
) -> None:
    publication = payload.get("ref_publication")
    if not isinstance(publication, dict):
        raise RuntimeError("Workspace ref publication evidence is invalid.")
    base = (
        "refs/crewplane/runs/"
        f"{safe_ref_component(_required_identity(payload, 'run_key_name'))}/"
        f"{safe_ref_component(_required_identity(payload, 'node_id'))}/"
        f"{safe_ref_component(_required_invocation_slug(payload))}"
    )
    expected = (f"{base}/candidate", f"{base}/result")
    if tuple(destination.name for destination in destinations) != expected:
        raise RuntimeError("Workspace result refs escape their invocation scope.")
    for ref_name in expected:
        normalized = command.text("check-ref-format", "--normalize", ref_name)
        if normalized != ref_name:
            raise RuntimeError(f"Unsafe workspace ref name: {ref_name}")


def _required_identity(payload: dict[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Workspace ref publication lacks {field} identity.")
    return value


def _required_invocation_slug(payload: dict[str, object]) -> str:
    node_id = _required_identity(payload, "node_id")
    task_id = _required_identity(payload, "task_id")
    round_num = payload.get("round_num")
    audit_round_num = payload.get("audit_round_num")
    if not isinstance(round_num, int) or isinstance(round_num, bool):
        raise RuntimeError("Workspace ref publication lacks round identity.")
    if audit_round_num is not None and (
        not isinstance(audit_round_num, int) or isinstance(audit_round_num, bool)
    ):
        raise RuntimeError("Workspace ref publication has invalid audit identity.")
    return invocation_slug(node_id, task_id, audit_round_num, round_num)


def _publication_destination(value: object) -> RefPublicationDestination:
    if not isinstance(value, dict):
        raise RuntimeError("Workspace ref publication destination is invalid.")
    name = value.get("name")
    target_oid = value.get("target_oid")
    expected_old_oid = value.get("expected_old_oid")
    if not isinstance(name, str) or not _is_object_id(target_oid):
        raise RuntimeError("Workspace ref publication destination is invalid.")
    if expected_old_oid is not None and not _is_object_id(expected_old_oid):
        raise RuntimeError("Workspace ref publication expected OID is invalid.")
    return RefPublicationDestination(name, target_oid, expected_old_oid)


def _persist_prepared_publication(
    request: WorktreeCaptureRequest,
    destinations: tuple[RefPublicationDestination, RefPublicationDestination],
) -> None:
    state = read_workspace_state(request.state_path)
    require_workspace_state_contract(state, "materialization")
    if not (
        state.get("run_key_name") == request.plan.run_key_name
        and state.get("node_id") == request.node_id
        and state.get("task_id") == request.task_id
        and isinstance(state.get("run_id"), str)
    ):
        raise RuntimeError("Workspace ref publication identity is contradictory.")
    publication = {
        "phase": "prepared",
        "repository_id": request.source.repository_id,
        "run_id": state.get("run_id"),
        "run_key_name": state["run_key_name"],
        "node_id": request.node_id,
        "task_id": request.task_id,
        "role": state.get("role"),
        "round_num": state.get("round_num"),
        "audit_round_num": state.get("audit_round_num"),
        "destinations": {
            "candidate": _destination_payload(destinations[0]),
            "result": _destination_payload(destinations[1]),
        },
    }
    update_workspace_ref_publication(request.state_path, publication)


def _destination_payload(destination: RefPublicationDestination) -> dict[str, object]:
    return {
        "name": destination.name,
        "target_oid": destination.target_oid,
        "expected_old_oid": destination.expected_old_oid,
    }


def _publish_transaction(
    command: GitCommand,
    destinations: tuple[RefPublicationDestination, RefPublicationDestination],
    protected_refs: ProtectedRefSnapshot,
) -> None:
    for destination in destinations:
        if _ref_oid(command, destination.name) is not None:
            raise RuntimeError(
                f"Workspace result ref already exists: {destination.name}."
            )
    lines = ["start"]
    destination_names = {destination.name for destination in destinations}
    expected_refs = dict(protected_refs.refs)
    zero_oid = "0" * len(destinations[0].target_oid)
    for ref_name in protected_refs.scopes:
        if ref_name in destination_names:
            continue
        lines.append("option no-deref")
        lines.append(f"verify {ref_name} {expected_refs.get(ref_name, zero_oid)}")
    for destination in destinations:
        lines.append("option no-deref")
        lines.append(f"update {destination.name} {destination.target_oid} {zero_oid}")
    lines.extend(("prepare", "commit", ""))
    command.run_with_input("\n".join(lines).encode(), "update-ref", "--stdin")


def _delete_transaction(
    command: GitCommand,
    destinations: tuple[RefPublicationDestination, ...],
) -> None:
    lines = ["start"]
    for destination in destinations:
        lines.append("option no-deref")
        lines.append(f"delete {destination.name} {destination.target_oid}")
    lines.extend(("prepare", "commit", ""))
    command.run_with_input("\n".join(lines).encode(), "update-ref", "--stdin")


def _ref_oid(command: GitCommand, ref_name: str) -> str | None:
    _reject_symbolic_ref(command, ref_name)
    try:
        return command.text("rev-parse", "--verify", ref_name)
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 128:
            return None
        raise


def _reject_symbolic_ref(command: GitCommand, ref_name: str) -> None:
    try:
        target = command.text("symbolic-ref", "-q", ref_name)
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 1:
            return
        raise
    raise RuntimeError(
        f"Workspace result ref is symbolic and was retained: {ref_name} -> {target}."
    )


def _result_ref_names(request: WorktreeCaptureRequest) -> tuple[str, str]:
    base = (
        "refs/crewplane/runs/"
        f"{safe_ref_component(request.plan.run_key_name)}/"
        f"{safe_ref_component(request.node_id)}/"
        f"{safe_ref_component(request.slug)}"
    )
    return (
        checked_ref(request.checkout_root, f"{base}/candidate"),
        checked_ref(request.checkout_root, f"{base}/result"),
    )


def _is_object_id(value: object) -> TypeIs[str]:
    return (
        isinstance(value, str)
        and len(value) in {40, 64}
        and all(char in "0123456789abcdef" for char in value)
    )
