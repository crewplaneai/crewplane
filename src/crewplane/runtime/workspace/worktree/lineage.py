from __future__ import annotations

import stat
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

from crewplane.artifacts.atomic import atomic_write_json
from crewplane.artifacts.workspace.chain_validation import (
    verify_workspace_source_chain,
)
from crewplane.core.file_hashing import sha256_file
from crewplane.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSourceSnapshot,
)
from crewplane.core.workspace.invocation_identity import invocation_slug

from ..cleanup_notes import note_cleanup_failure
from ..git import GitCommand, git
from ..locks import git_metadata_lock
from ..state import (
    mark_workspace_temporary_ref_removed,
    read_workspace_state,
    record_workspace_temporary_ref,
)
from .ref_publication import (
    publish_result_refs,
    reconcile_result_ref_publication,
)
from .refs import checked_ref, safe_file_component, safe_ref_component
from .types import WorktreeCaptureRequest, WorktreeSourceRef


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
        evidence_path = evidence_dir / f"workspace-temporary-refs-{uuid4().hex}.json"
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
class _TemporaryImportRef:
    source: WorkspaceSourceSnapshot
    owner: TemporaryRefOwner
    name: str
    target_oid: str


@contextmanager
def ensure_source_commit_available(
    source: WorkspaceSourceSnapshot,
    source_ref: WorktreeSourceRef,
    owner: TemporaryRefOwner | None = None,
    cancel_requested: Callable[[], bool] | None = None,
    source_chain_verified: bool = False,
) -> Iterator[None]:
    if not source_chain_verified:
        _verify_source_chain(source, source_ref)
    imported_refs: list[_TemporaryImportRef] = []
    try:
        _ensure_source_commit_available(
            source,
            source_ref,
            set(),
            owner,
            cancel_requested,
            imported_refs,
        )
        yield
    except BaseException as failure:
        try:
            _delete_temporary_import_refs(imported_refs, cancel_requested)
        except Exception as cleanup_error:
            note_cleanup_failure(
                failure,
                "Workspace temporary import ref cleanup",
                cleanup_error,
            )
        raise
    else:
        _delete_temporary_import_refs(imported_refs, cancel_requested)
    finally:
        if owner is not None:
            owner.discard_resolved_evidence()


def verify_source_commit_available(
    source: WorkspaceSourceSnapshot,
    source_ref: WorktreeSourceRef,
) -> None:
    _verify_source_chain(source, source_ref)


def _verify_source_chain(
    source: WorkspaceSourceSnapshot,
    source_ref: WorktreeSourceRef,
) -> None:
    try:
        verify_workspace_source_chain(source, source_ref)
    except RuntimeError as exc:
        message = str(exc)
        if message.startswith("Workspace lineage source verification failed"):
            raise
        raise RuntimeError(
            "Workspace lineage source verification failed while validating "
            f"recorded Git artifacts: {message}"
        ) from exc


def _ensure_source_commit_available(
    source: WorkspaceSourceSnapshot,
    source_ref: WorktreeSourceRef,
    active_commits: set[str],
    owner: TemporaryRefOwner | None,
    cancel_requested: Callable[[], bool] | None,
    imported_refs: list[_TemporaryImportRef],
) -> None:
    if source_ref.source_commit in active_commits:
        raise RuntimeError("Workspace lineage source chain contains a cycle.")
    active_commits.add(source_ref.source_commit)
    for upstream in source_ref.upstream_sources:
        _ensure_source_commit_available(
            source,
            upstream,
            active_commits,
            owner,
            cancel_requested,
            imported_refs,
        )
    active_commits.remove(source_ref.source_commit)

    verified_bundle_path: Path | None = None
    if _source_requires_bundle(source_ref):
        verified_bundle_path = _verify_source_bundle_descriptor(
            git(Path(source.git_top_level)),
            source_ref,
        )
    if verified_bundle_path is None and _source_commit_exists(
        source,
        source_ref.source_commit,
    ):
        _reject_source_tree_mismatch(source, source_ref)
        return
    if verified_bundle_path is None:
        raise RuntimeError(
            "Workspace lineage source commit is unavailable and no bundle "
            "descriptor was recorded."
        )
    imported_refs.append(
        _import_source_bundle(
            source,
            source_ref,
            verified_bundle_path,
            owner,
            cancel_requested,
        )
    )
    if not _source_commit_exists(source, source_ref.source_commit):
        raise RuntimeError(
            "Workspace lineage source bundle import did not provide the expected "
            "commit."
        )
    _reject_source_tree_mismatch(source, source_ref)


def update_result_refs(
    request: WorktreeCaptureRequest,
    candidate_commit: str,
    result_commit: str,
    cancel_requested: Callable[[], bool] | None = None,
) -> tuple[str, str]:
    return publish_result_refs(
        request,
        candidate_commit,
        result_commit,
        cancel_requested,
    )


def delete_result_refs(
    request: WorktreeCaptureRequest,
    cancel_requested: Callable[[], bool] | None = None,
) -> None:
    reconcile_result_ref_publication(
        request.state_path,
        Path(request.source.git_top_level),
        Path(request.source.common_git_dir),
        cancel_requested,
    )


def cleanup_result_refs_after_failure(
    request: WorktreeCaptureRequest,
    failure: BaseException,
    cancel_requested: Callable[[], bool] | None = None,
) -> None:
    try:
        delete_result_refs(request, cancel_requested)
    except Exception as cleanup_error:
        note_cleanup_failure(
            failure,
            "Workspace result ref cleanup after capture failure",
            cleanup_error,
        )


def export_bundle(
    request: WorktreeCaptureRequest,
    result_ref: str,
    cancel_requested: Callable[[], bool] | None = None,
) -> Path:
    bundle_dir = request.state_path.parent / "workspace-bundles"
    _ensure_safe_bundle_dir(request.state_path.parent, bundle_dir)
    bundle_dir.chmod(0o700)
    bundle_path = bundle_dir / f"{safe_file_component(request.slug)}.bundle"
    temp_bundle_path = _temporary_bundle_path(bundle_dir, request.slug)
    try:
        with git_metadata_lock(
            Path(request.source.common_git_dir),
            cancel_requested,
        ):
            command = git(request.checkout_root)
            _reject_unsafe_existing_bundle_path(bundle_path)
            _reject_unsafe_bundle_file(temp_bundle_path, "temporary bundle")
            command.run("bundle", "create", temp_bundle_path.as_posix(), result_ref)
            _reject_unsafe_bundle_file(temp_bundle_path, "temporary bundle")
            command.run("bundle", "verify", temp_bundle_path.as_posix())
            _reject_unsafe_existing_bundle_path(bundle_path)
            temp_bundle_path.replace(bundle_path)
            _reject_unsafe_bundle_file(bundle_path, "workspace bundle")
            command.run("bundle", "verify", bundle_path.as_posix())
    except Exception:
        _unlink_best_effort(temp_bundle_path)
        raise
    return bundle_path


def _temporary_bundle_path(bundle_dir: Path, slug: str) -> Path:
    with NamedTemporaryFile(
        prefix=f".{safe_file_component(slug)}-",
        suffix=".bundle.tmp",
        dir=bundle_dir,
        delete=False,
    ) as temp_file:
        return Path(temp_file.name)


def _reject_unsafe_existing_bundle_path(bundle_path: Path) -> None:
    try:
        mode = bundle_path.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode):
        raise RuntimeError(
            f"Workspace bundle path must not be a symlink: {bundle_path.as_posix()}."
        )
    if not stat.S_ISREG(mode):
        raise RuntimeError(
            f"Workspace bundle path must be a regular file: {bundle_path.as_posix()}."
        )


def _reject_unsafe_bundle_file(bundle_path: Path, label: str) -> None:
    try:
        mode = bundle_path.lstat().st_mode
        file_stat = bundle_path.stat()
    except FileNotFoundError as exc:
        raise RuntimeError(f"Workspace {label} is missing.") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode) or file_stat.st_nlink != 1:
        raise RuntimeError(f"Workspace {label} must be a private regular file.")


def _unlink_best_effort(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except Exception:
        return


def _ensure_safe_bundle_dir(stage_dir: Path, bundle_dir: Path) -> None:
    _reject_unsafe_artifact_dir(stage_dir)
    try:
        mode = bundle_dir.lstat().st_mode
    except FileNotFoundError:
        bundle_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        _reject_unsafe_artifact_dir(bundle_dir)
        return
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise RuntimeError("Workspace bundle directory must be a real directory.")
    _reject_unsafe_artifact_dir(bundle_dir)
    resolved_stage = stage_dir.resolve(strict=True)
    resolved_bundle = bundle_dir.resolve(strict=True)
    if not resolved_bundle.is_relative_to(resolved_stage):
        raise RuntimeError("Workspace bundle directory escapes the stage directory.")


def _reject_unsafe_artifact_dir(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Workspace artifact directory is missing: {path.as_posix()}."
        ) from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise RuntimeError(
            f"Workspace artifact directory must be a real directory: {path.as_posix()}."
        )


def worktree_protected_ref_scopes(
    plan: PreflightExecutionPlan,
    source_ref: WorktreeSourceRef,
    node_id: str,
    slug: str,
) -> tuple[str, ...]:
    destination_base = (
        "refs/crewplane/runs/"
        f"{safe_ref_component(plan.run_key_name)}/"
        f"{safe_ref_component(node_id)}/"
        f"{safe_ref_component(slug)}"
    )
    refs = {
        f"{destination_base}/candidate",
        f"{destination_base}/result",
    }
    pending = [source_ref]
    while pending:
        current = pending.pop()
        if current.bundle_ref is not None:
            refs.add(current.bundle_ref)
        pending.extend(current.upstream_sources)
    return tuple(sorted(refs))


def _source_commit_exists(source: WorkspaceSourceSnapshot, commit: str) -> bool:
    try:
        git(Path(source.git_top_level)).run("cat-file", "-e", f"{commit}^{{commit}}")
    except subprocess.CalledProcessError:
        return False
    return True


def _source_requires_bundle(source_ref: WorktreeSourceRef) -> bool:
    return source_ref.source_kind in {"node", "candidate"}


def _reject_source_tree_mismatch(
    source: WorkspaceSourceSnapshot,
    source_ref: WorktreeSourceRef,
) -> None:
    actual_tree = git(Path(source.git_top_level)).text(
        "rev-parse",
        f"{source_ref.source_commit}^{{tree}}",
    )
    if actual_tree == source_ref.source_tree:
        return
    raise RuntimeError(
        "Workspace lineage source tree mismatch: recorded source tree "
        f"{source_ref.source_tree} does not match commit "
        f"{source_ref.source_commit} tree {actual_tree}."
    )


def _verify_source_bundle_descriptor(
    command: GitCommand,
    source_ref: WorktreeSourceRef,
) -> Path:
    bundle_path = source_ref.bundle_path
    if bundle_path is None:
        raise RuntimeError("Workspace lineage bundle path is missing.")
    if not bundle_path.is_file():
        raise RuntimeError(
            f"Workspace lineage bundle is missing: {bundle_path.as_posix()}."
        )
    if source_ref.bundle_sha256 is None:
        raise RuntimeError("Workspace lineage bundle digest is missing.")
    digest = sha256_file(bundle_path)
    if digest != source_ref.bundle_sha256:
        raise RuntimeError("Workspace lineage bundle digest mismatch.")
    if source_ref.bundle_size_bytes is None:
        raise RuntimeError("Workspace lineage bundle size is missing.")
    if bundle_path.stat().st_size != source_ref.bundle_size_bytes:
        raise RuntimeError("Workspace lineage bundle size mismatch.")
    if source_ref.bundle_ref is None:
        raise RuntimeError("Workspace lineage bundle ref is missing.")
    command.run("bundle", "verify", bundle_path.as_posix())
    _reject_bundle_ref_mismatch(command, bundle_path, source_ref)
    return bundle_path


def _reject_bundle_ref_mismatch(
    command: GitCommand,
    bundle_path: Path,
    source_ref: WorktreeSourceRef,
) -> None:
    listed = command.text(
        "bundle",
        "list-heads",
        bundle_path.as_posix(),
        source_ref.bundle_ref or "",
    )
    lines = listed.splitlines()
    if len(lines) != 1:
        raise RuntimeError("Workspace lineage bundle ref mismatch.")
    object_id, separator, ref_name = lines[0].partition(" ")
    if (
        separator == " "
        and object_id == source_ref.source_commit
        and ref_name == source_ref.bundle_ref
    ):
        return
    raise RuntimeError("Workspace lineage bundle ref mismatch.")


def _import_source_bundle(
    source: WorkspaceSourceSnapshot,
    source_ref: WorktreeSourceRef,
    verified_bundle_path: Path | None,
    owner: TemporaryRefOwner | None,
    cancel_requested: Callable[[], bool] | None,
) -> _TemporaryImportRef:
    command = git(Path(source.git_top_level))
    bundle_path = verified_bundle_path or _verify_source_bundle_descriptor(
        command,
        source_ref,
    )
    if owner is None:
        raise RuntimeError(
            "Workspace source import requires durable invocation-owned cleanup evidence."
        )
    owner.prepare()
    state = read_workspace_state(owner.state_path)
    import_ref = checked_ref(
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
        import_ref,
        source_ref.source_commit,
    )
    imported_ref = _TemporaryImportRef(
        source,
        owner,
        import_ref,
        source_ref.source_commit,
    )
    try:
        with git_metadata_lock(Path(source.common_git_dir), cancel_requested):
            if _direct_ref_oid(command, import_ref) is not None:
                raise RuntimeError(
                    f"Workspace temporary import ref already exists: {import_ref}."
                )
            command.run("bundle", "unbundle", bundle_path.as_posix())
            zero_oid = "0" * len(source_ref.source_commit)
            command.run(
                "update-ref",
                "--no-deref",
                import_ref,
                source_ref.source_commit,
                zero_oid,
            )
        if not _source_commit_exists(source, source_ref.source_commit):
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


def _import_ref_for_source_commit(
    run_key_name: str,
    node_id: str,
    invocation_slug: str,
    source_commit: str,
) -> str:
    import_id = uuid4().hex[:16]
    return (
        "refs/crewplane/runs/"
        f"{safe_ref_component(run_key_name)}/imports/"
        f"{safe_ref_component(node_id)}/"
        f"{safe_ref_component(invocation_slug)}/"
        f"{safe_ref_component(source_commit[:24])}-{import_id}"
    )


def _delete_temporary_import_ref(
    imported_ref: _TemporaryImportRef,
    cancel_requested: Callable[[], bool] | None,
) -> None:
    source = imported_ref.source
    ref_name = imported_ref.name
    target_oid = imported_ref.target_oid
    with git_metadata_lock(Path(source.common_git_dir), cancel_requested):
        command = git(Path(source.git_top_level))
        current = _direct_ref_oid(command, ref_name)
        if current == target_oid:
            command.run("update-ref", "--no-deref", "-d", ref_name, target_oid)
        elif current is not None:
            raise RuntimeError(
                f"Workspace temporary import ref moved and was retained: {ref_name}."
            )
    mark_workspace_temporary_ref_removed(imported_ref.owner.state_path, ref_name)


def _delete_temporary_import_refs(
    imported_refs: list[_TemporaryImportRef],
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
    removed = 0
    prefix = _temporary_ref_owner_prefix(payload)
    for claim in claims:
        if not isinstance(claim, dict):
            raise RuntimeError("Workspace temporary ref cleanup claim is invalid.")
        if claim.get("phase") == "removed":
            continue
        ref_name = claim.get("name")
        target_oid = claim.get("target_oid")
        if not isinstance(ref_name, str) or not isinstance(target_oid, str):
            raise RuntimeError("Workspace temporary ref cleanup claim is incomplete.")
        if not ref_name.startswith(prefix):
            raise RuntimeError(
                "Workspace temporary ref cleanup claim escapes its invocation scope."
            )
        with git_metadata_lock(common_git_dir):
            command = git(repo_root)
            if command.text("check-ref-format", "--normalize", ref_name) != ref_name:
                raise RuntimeError("Workspace temporary ref cleanup name is unsafe.")
            current = _direct_ref_oid(command, ref_name)
            if current == target_oid:
                command.run("update-ref", "--no-deref", "-d", ref_name, target_oid)
                removed += 1
            elif current is not None:
                raise RuntimeError(
                    "Workspace temporary import ref moved and was retained: "
                    f"{ref_name}."
                )
        mark_workspace_temporary_ref_removed(state_path, ref_name)
    return removed


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
        values.append(safe_ref_component(value))
    return f"refs/crewplane/runs/{values[0]}/imports/{values[1]}/{values[2]}/"


def _state_invocation_slug(payload: dict[str, object]) -> str:
    node_id = payload.get("node_id")
    task_id = payload.get("task_id")
    round_num = payload.get("round_num")
    audit_round_num = payload.get("audit_round_num")
    if not (
        isinstance(node_id, str)
        and node_id
        and isinstance(task_id, str)
        and task_id
        and isinstance(round_num, int)
        and not isinstance(round_num, bool)
        and (
            audit_round_num is None
            or isinstance(audit_round_num, int)
            and not isinstance(audit_round_num, bool)
        )
    ):
        raise RuntimeError(
            "Workspace temporary ref evidence lacks invocation identity."
        )
    return invocation_slug(node_id, task_id, audit_round_num, round_num)


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}
