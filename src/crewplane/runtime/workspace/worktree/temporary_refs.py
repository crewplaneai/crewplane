from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from crewplane.artifacts.atomic import atomic_write_json
from crewplane.artifacts.workspace.state.invocation import state_invocation_slug
from crewplane.artifacts.workspace.state.paths import workspace_temporary_refs_filename
from crewplane.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSourceSnapshot,
)
from crewplane.core.workspace.naming import (
    safe_ref_component,
    temporary_import_ref_prefix,
)

from ..cleanup_notes import note_cleanup_failure
from ..git import GitCommand, git
from ..locks import git_metadata_lock
from ..state import read_workspace_state
from ..state_evidence import (
    mark_workspace_temporary_ref_removed,
    record_workspace_temporary_ref,
)
from .refs import checked_ref
from .types import WorktreeSourceRef


@dataclass(frozen=True)
class TemporaryRefOwner:
    state_path: Path
    initial_payload: dict[str, object] | None = None

    @classmethod
    def dedicated(
        cls,
        plan: PreflightExecutionPlan,
        source: WorkspaceSourceSnapshot,
        evidence_dir: Path,
        node_id: str,
        consumer_id: str,
    ) -> TemporaryRefOwner:
        evidence_path = evidence_dir / workspace_temporary_refs_filename(uuid4().hex)
        return cls(
            evidence_path,
            {
                "evidence_kind": "temporary_ref_cleanup",
                "run_id": plan.run_id,
                "run_key_name": plan.run_key_name,
                "node_id": node_id,
                "task_id": consumer_id,
                "role": "artifact_consumer",
                "round_num": 0,
                "audit_round_num": None,
                "git": {"repo_id": source.repository_id},
                "temporary_refs": [],
            },
        )

    def prepare(self) -> None:
        if self.initial_payload is None or self.state_path.exists():
            return
        atomic_write_json(self.state_path, self.initial_payload)

    def discard_resolved_evidence(self) -> None:
        if self.initial_payload is None or not self.state_path.is_file():
            return
        payload = read_workspace_state(self.state_path)
        claims = payload.get("temporary_refs")
        if isinstance(claims, list) and all(
            isinstance(claim, dict) and claim.get("phase") == "removed"
            for claim in claims
        ):
            self.state_path.unlink()


@dataclass(frozen=True)
class TemporaryImportRef:
    source: WorkspaceSourceSnapshot
    owner: TemporaryRefOwner
    name: str
    target_oid: str


def import_source_bundle(
    source: WorkspaceSourceSnapshot,
    source_ref: WorktreeSourceRef,
    verified_bundle_path: Path,
    owner: TemporaryRefOwner | None,
    cancel_requested: Callable[[], bool] | None,
) -> TemporaryImportRef:
    imported_ref = _prepare_temporary_import_ref(source, source_ref, owner)
    try:
        _create_temporary_import_ref(
            imported_ref,
            verified_bundle_path,
            cancel_requested,
        )
        if not git(Path(source.git_top_level)).commit_exists(source_ref.source_commit):
            raise RuntimeError("Workspace source import did not provide its target.")
    except BaseException as failure:
        try:
            _delete_temporary_import_ref(imported_ref, cancel_requested)
        except Exception as cleanup_error:
            note_cleanup_failure(
                failure,
                "Workspace temporary import ref cleanup",
                cleanup_error,
            )
        raise
    return imported_ref


def _prepare_temporary_import_ref(
    source: WorkspaceSourceSnapshot,
    source_ref: WorktreeSourceRef,
    owner: TemporaryRefOwner | None,
) -> TemporaryImportRef:
    if owner is None:
        raise RuntimeError(
            "Workspace source import requires durable invocation-owned cleanup evidence."
        )
    owner.prepare()
    state = read_workspace_state(owner.state_path)
    ref_name = checked_ref(
        Path(source.git_top_level),
        _import_ref_for_source_commit(
            str(state.get("run_key_name")),
            str(state.get("node_id")),
            _state_invocation_slug(state),
            source_ref.source_commit,
        ),
    )
    record_workspace_temporary_ref(
        owner.state_path,
        ref_name,
        source_ref.source_commit,
    )
    return TemporaryImportRef(source, owner, ref_name, source_ref.source_commit)


def _create_temporary_import_ref(
    imported_ref: TemporaryImportRef,
    bundle_path: Path,
    cancel_requested: Callable[[], bool] | None,
) -> None:
    source = imported_ref.source
    command = git(Path(source.git_top_level))
    with git_metadata_lock(Path(source.common_git_dir), cancel_requested):
        if _direct_ref_oid(command, imported_ref.name) is not None:
            raise RuntimeError(
                f"Workspace temporary import ref already exists: {imported_ref.name}."
            )
        command.run("bundle", "unbundle", bundle_path.as_posix())
        zero_oid = "0" * len(imported_ref.target_oid)
        command.run(
            "update-ref",
            "--no-deref",
            imported_ref.name,
            imported_ref.target_oid,
            zero_oid,
        )


def _import_ref_for_source_commit(
    run_key_name: str,
    node_id: str,
    invocation_slug: str,
    source_commit: str,
) -> str:
    import_id = uuid4().hex[:16]
    prefix = temporary_import_ref_prefix(run_key_name, node_id, invocation_slug)
    return f"{prefix}{safe_ref_component(source_commit[:24])}-{import_id}"


def delete_temporary_import_refs(
    imported_refs: list[TemporaryImportRef],
    cancel_requested: Callable[[], bool] | None,
) -> None:
    first_failure: Exception | None = None
    for imported_ref in reversed(imported_refs):
        try:
            _delete_temporary_import_ref(imported_ref, cancel_requested)
        except Exception as cleanup_error:
            if first_failure is None:
                first_failure = cleanup_error
            else:
                note_cleanup_failure(
                    first_failure,
                    "Additional workspace temporary import ref cleanup",
                    cleanup_error,
                )
    if first_failure is not None:
        raise first_failure


def _delete_temporary_import_ref(
    imported_ref: TemporaryImportRef,
    cancel_requested: Callable[[], bool] | None,
) -> None:
    source = imported_ref.source
    with git_metadata_lock(Path(source.common_git_dir), cancel_requested):
        command = git(Path(source.git_top_level))
        current = _direct_ref_oid(command, imported_ref.name)
        if current == imported_ref.target_oid:
            command.run(
                "update-ref",
                "--no-deref",
                "-d",
                imported_ref.name,
                imported_ref.target_oid,
            )
        elif current is not None:
            raise RuntimeError(
                "Workspace temporary import ref moved and was retained: "
                f"{imported_ref.name}."
            )
    mark_workspace_temporary_ref_removed(
        imported_ref.owner.state_path,
        imported_ref.name,
    )


def reconcile_temporary_import_refs(
    state_path: Path,
    repo_root: Path,
    common_git_dir: Path,
    expected_repository_id: str,
) -> int:
    payload = read_workspace_state(state_path)
    _require_temporary_ref_repository_identity(payload, expected_repository_id)
    claims = payload.get("temporary_refs")
    if claims is None:
        return 0
    if not isinstance(claims, list):
        raise RuntimeError("Workspace temporary ref cleanup evidence is invalid.")
    prefix = _temporary_ref_owner_prefix(payload)
    removed = 0
    for claim in claims:
        removed += _reconcile_temporary_import_ref_claim(
            state_path,
            repo_root,
            common_git_dir,
            claim,
            prefix,
        )
    return removed


def _reconcile_temporary_import_ref_claim(
    state_path: Path,
    repo_root: Path,
    common_git_dir: Path,
    claim: object,
    prefix: str,
) -> int:
    if not isinstance(claim, dict):
        raise RuntimeError("Workspace temporary ref cleanup claim is invalid.")
    if claim.get("phase") == "removed":
        return 0
    ref_name, target_oid = _temporary_ref_claim_identity(claim, prefix)
    removed = _reconcile_temporary_import_ref(
        repo_root,
        common_git_dir,
        ref_name,
        target_oid,
    )
    mark_workspace_temporary_ref_removed(state_path, ref_name)
    return removed


def _temporary_ref_claim_identity(
    claim: dict[str, object],
    prefix: str,
) -> tuple[str, str]:
    ref_name = claim.get("name")
    target_oid = claim.get("target_oid")
    if not isinstance(ref_name, str) or not isinstance(target_oid, str):
        raise RuntimeError("Workspace temporary ref cleanup claim is incomplete.")
    if not ref_name.startswith(prefix):
        raise RuntimeError(
            "Workspace temporary ref cleanup claim escapes its invocation scope."
        )
    return ref_name, target_oid


def _reconcile_temporary_import_ref(
    repo_root: Path,
    common_git_dir: Path,
    ref_name: str,
    target_oid: str,
) -> int:
    with git_metadata_lock(common_git_dir):
        command = git(repo_root)
        if command.text("check-ref-format", "--normalize", ref_name) != ref_name:
            raise RuntimeError("Workspace temporary ref cleanup name is unsafe.")
        current = _direct_ref_oid(command, ref_name)
        if current == target_oid:
            command.run("update-ref", "--no-deref", "-d", ref_name, target_oid)
            return 1
        if current is not None:
            raise RuntimeError(
                f"Workspace temporary import ref moved and was retained: {ref_name}."
            )
    return 0


def _require_temporary_ref_repository_identity(
    payload: dict[str, object],
    expected_repository_id: str,
) -> None:
    git_payload = payload.get("git")
    claims = payload.get("temporary_refs")
    if (
        not isinstance(git_payload, dict)
        or git_payload.get("repo_id") != expected_repository_id
    ):
        raise RuntimeError(
            "Workspace temporary ref cleanup repository identity changed."
        )
    if isinstance(claims, list) and any(
        not isinstance(claim, dict)
        or claim.get("repository_id") != expected_repository_id
        for claim in claims
    ):
        raise RuntimeError(
            "Workspace temporary ref cleanup claim repository identity changed."
        )


def _direct_ref_oid(command: GitCommand, ref_name: str) -> str | None:
    try:
        target = command.text("symbolic-ref", "-q", ref_name)
    except subprocess.CalledProcessError as exc:
        if exc.returncode != 1:
            raise
    else:
        raise RuntimeError(
            "Workspace temporary import ref is symbolic and was retained: "
            f"{ref_name} -> {target}."
        )
    try:
        return command.text("rev-parse", "--verify", ref_name)
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 128:
            return None
        raise


def _temporary_ref_owner_prefix(payload: dict[str, object]) -> str:
    values: list[str] = []
    identities = (
        ("run_key_name", payload.get("run_key_name")),
        ("node_id", payload.get("node_id")),
        ("invocation_slug", _state_invocation_slug(payload)),
    )
    for field, value in identities:
        if not isinstance(value, str) or not value:
            raise RuntimeError(f"Workspace temporary ref evidence lacks {field}.")
        values.append(value)
    return temporary_import_ref_prefix(values[0], values[1], values[2])


def _state_invocation_slug(payload: dict[str, object]) -> str:
    slug = state_invocation_slug(payload)
    if slug is None:
        raise RuntimeError(
            "Workspace temporary ref evidence lacks invocation identity."
        )
    return slug
