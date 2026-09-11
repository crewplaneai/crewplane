from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from crewplane.artifacts.workspace.state.contracts import (
    require_workspace_state_contract,
)
from crewplane.artifacts.workspace.state.invocation import (
    InvalidInvocationField,
    require_state_invocation_slug,
)
from crewplane.core.workspace.git_policy import is_git_object_id
from crewplane.core.workspace.naming import result_ref_names

from ..cleanup_notes import note_cleanup_failure
from ..git import GitCommand, git
from ..locks import git_metadata_lock
from ..state import read_workspace_state
from ..state_evidence import (
    update_workspace_ref_publication,
    update_workspace_ref_publication_phase,
)
from .protected_refs import ProtectedRefSnapshot
from .refs import checked_ref
from .types import WorktreeCaptureRequest


@dataclass(frozen=True)
class RefPublicationDestination:
    name: str
    target_oid: str
    expected_old_oid: str | None


type _ResultRefDestinations = tuple[
    RefPublicationDestination,
    RefPublicationDestination,
]


@dataclass(frozen=True, slots=True)
class _PublicationReconciliation:
    removed: tuple[RefPublicationDestination, ...]
    mismatched: tuple[RefPublicationDestination, ...]
    remaining: tuple[RefPublicationDestination, ...]


def publish_result_refs(
    request: WorktreeCaptureRequest,
    candidate_commit: str,
    result_commit: str,
    cancel_requested: Callable[[], bool] | None = None,
) -> tuple[str, str]:
    """Persist and publish candidate and result refs as one transaction."""

    destinations = _result_ref_destinations(
        request,
        candidate_commit,
        result_commit,
    )
    _persist_prepared_publication(request, destinations)
    _publish_destinations(request, destinations, cancel_requested)
    _record_published_phase(request, cancel_requested)
    return destinations[0].name, destinations[1].name


def _result_ref_destinations(
    request: WorktreeCaptureRequest,
    candidate_commit: str,
    result_commit: str,
) -> _ResultRefDestinations:
    candidate_ref, result_ref = _result_ref_names(request)
    return (
        RefPublicationDestination(candidate_ref, candidate_commit, None),
        RefPublicationDestination(result_ref, result_commit, None),
    )


def _publish_destinations(
    request: WorktreeCaptureRequest,
    destinations: _ResultRefDestinations,
    cancel_requested: Callable[[], bool] | None,
) -> None:
    with git_metadata_lock(Path(request.source.common_git_dir), cancel_requested):
        command = git(request.checkout_root)
        _publish_transaction(command, destinations, request.protected_refs)


def _record_published_phase(
    request: WorktreeCaptureRequest,
    cancel_requested: Callable[[], bool] | None,
) -> None:
    try:
        update_workspace_ref_publication_phase(request.state_path, "published")
    except Exception as exc:
        _reconcile_after_phase_failure(request, cancel_requested, exc)
        raise


def _reconcile_after_phase_failure(
    request: WorktreeCaptureRequest,
    cancel_requested: Callable[[], bool] | None,
    phase_error: Exception,
) -> None:
    try:
        reconcile_result_ref_publication(
            request.state_path,
            Path(request.source.git_top_level),
            Path(request.source.common_git_dir),
            cancel_requested,
        )
    except Exception as cleanup_error:
        note_cleanup_failure(
            phase_error,
            "Workspace result ref cleanup after publication phase failure",
            cleanup_error,
        )


def reconcile_result_ref_publication(
    state_path: Path,
    repo_root: Path,
    common_git_dir: Path,
    cancel_requested: Callable[[], bool] | None = None,
) -> int:
    """Remove still-owned result refs and record their publication as removed."""

    payload = read_workspace_state(state_path)
    _require_repository_identity(payload, repo_root, common_git_dir)
    destinations = publication_destinations(payload)
    if not destinations:
        return 0
    with git_metadata_lock(common_git_dir, cancel_requested):
        command = git(repo_root)
        reconciliation = _reconcile_destinations(command, payload, destinations)
    _require_complete_reconciliation(reconciliation)
    update_workspace_ref_publication_phase(state_path, "removed")
    return len(reconciliation.removed)


def _reconcile_destinations(
    command: GitCommand,
    payload: dict[str, object],
    destinations: tuple[RefPublicationDestination, ...],
) -> _PublicationReconciliation:
    _require_owned_publication_destinations(command, payload, destinations)
    matching = _matching_destinations(command, destinations)
    mismatched = _mismatched_destinations(command, destinations)
    if matching:
        _delete_transaction(command, matching)
    remaining = _remaining_destinations(command, destinations)
    return _PublicationReconciliation(
        removed=matching,
        mismatched=mismatched,
        remaining=remaining,
    )


def _matching_destinations(
    command: GitCommand,
    destinations: tuple[RefPublicationDestination, ...],
) -> tuple[RefPublicationDestination, ...]:
    return tuple(
        destination
        for destination in destinations
        if _ref_oid(command, destination.name) == destination.target_oid
    )


def _mismatched_destinations(
    command: GitCommand,
    destinations: tuple[RefPublicationDestination, ...],
) -> tuple[RefPublicationDestination, ...]:
    mismatched: list[RefPublicationDestination] = []
    for destination in destinations:
        current = _ref_oid(command, destination.name)
        if current is not None and current != destination.target_oid:
            mismatched.append(destination)
    return tuple(mismatched)


def _remaining_destinations(
    command: GitCommand,
    destinations: tuple[RefPublicationDestination, ...],
) -> tuple[RefPublicationDestination, ...]:
    return tuple(
        destination
        for destination in destinations
        if _ref_oid(command, destination.name) is not None
    )


def _require_complete_reconciliation(
    reconciliation: _PublicationReconciliation,
) -> None:
    unresolved = (*reconciliation.mismatched, *reconciliation.remaining)
    if not unresolved:
        return
    names = ", ".join(destination.name for destination in unresolved)
    raise RuntimeError(
        f"Workspace result refs moved or remain after exact-OID cleanup: {names}."
    )


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
    expected = result_ref_names(
        _required_identity(payload, "run_key_name"),
        _required_identity(payload, "node_id"),
        _required_invocation_slug(payload),
    )
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
    try:
        return require_state_invocation_slug(payload)
    except InvalidInvocationField as exc:
        if exc.field == "round_num":
            message = "Workspace ref publication lacks round identity."
        elif exc.field == "audit_round_num":
            message = "Workspace ref publication has invalid audit identity."
        else:
            message = f"Workspace ref publication lacks {exc.field} identity."
        raise RuntimeError(message) from exc


def _publication_destination(value: object) -> RefPublicationDestination:
    if not isinstance(value, dict):
        raise RuntimeError("Workspace ref publication destination is invalid.")
    name = value.get("name")
    target_oid = value.get("target_oid")
    expected_old_oid = value.get("expected_old_oid")
    if not isinstance(name, str) or not is_git_object_id(target_oid):
        raise RuntimeError("Workspace ref publication destination is invalid.")
    if expected_old_oid is not None and not is_git_object_id(expected_old_oid):
        raise RuntimeError("Workspace ref publication expected OID is invalid.")
    return RefPublicationDestination(name, target_oid, expected_old_oid)


def _persist_prepared_publication(
    request: WorktreeCaptureRequest,
    destinations: _ResultRefDestinations,
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
    destinations: _ResultRefDestinations,
    protected_refs: ProtectedRefSnapshot,
) -> None:
    _require_absent_destinations(command, destinations)
    zero_oid = "0" * len(destinations[0].target_oid)
    operations = (
        *_protected_ref_verification_operations(
            destinations,
            protected_refs,
            zero_oid,
        ),
        *_publication_update_operations(destinations, zero_oid),
    )
    _run_ref_transaction(command, operations)


def _require_absent_destinations(
    command: GitCommand,
    destinations: _ResultRefDestinations,
) -> None:
    for destination in destinations:
        if _ref_oid(command, destination.name) is not None:
            raise RuntimeError(
                f"Workspace result ref already exists: {destination.name}."
            )


def _protected_ref_verification_operations(
    destinations: _ResultRefDestinations,
    protected_refs: ProtectedRefSnapshot,
    zero_oid: str,
) -> tuple[str, ...]:
    destination_names = {destination.name for destination in destinations}
    expected_refs = dict(protected_refs.refs)
    return tuple(
        f"verify {ref_name} {expected_refs.get(ref_name, zero_oid)}"
        for ref_name in protected_refs.scopes
        if ref_name not in destination_names
    )


def _publication_update_operations(
    destinations: _ResultRefDestinations,
    zero_oid: str,
) -> tuple[str, ...]:
    return tuple(
        f"update {destination.name} {destination.target_oid} {zero_oid}"
        for destination in destinations
    )


def _delete_transaction(
    command: GitCommand,
    destinations: tuple[RefPublicationDestination, ...],
) -> None:
    operations = tuple(
        f"delete {destination.name} {destination.target_oid}"
        for destination in destinations
    )
    _run_ref_transaction(command, operations)


def _run_ref_transaction(command: GitCommand, operations: tuple[str, ...]) -> None:
    lines = ["start"]
    for operation in operations:
        lines.extend(("option no-deref", operation))
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
    candidate, result = result_ref_names(
        request.plan.run_key_name, request.node_id, request.slug
    )
    return (
        checked_ref(request.checkout_root, candidate),
        checked_ref(request.checkout_root, result),
    )
