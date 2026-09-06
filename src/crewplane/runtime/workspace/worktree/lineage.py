from __future__ import annotations

import stat
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

from crewplane.artifacts.workspace.chain_validation import (
    verify_workspace_source_chain,
)
from crewplane.core.file_hashing import sha256_file
from crewplane.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSourceSnapshot,
)

from ..cleanup_notes import note_cleanup_failure
from ..git import GitCommand, git
from ..locks import git_metadata_lock
from .ref_publication import (
    publish_result_refs,
    reconcile_result_ref_publication,
)
from .refs import safe_file_component, safe_ref_component
from .temporary_refs import (
    TemporaryImportRef,
    TemporaryRefOwner,
    delete_temporary_import_refs,
    import_source_bundle,
)
from .types import WorktreeCaptureRequest, WorktreeSourceRef


@contextmanager
def ensure_source_commit_available(
    source: WorkspaceSourceSnapshot,
    source_ref: WorktreeSourceRef,
    owner: TemporaryRefOwner | None = None,
    cancel_requested: Callable[[], bool] | None = None,
    source_chain_verified: bool = False,
) -> Iterator[None]:
    """Temporarily root every bundled commit needed by a source consumer.

    Bundled sources require an owner so cleanup claims are durable before Git is
    mutated. Unless the caller already verified the recorded source chain, the
    chain is verified before any imports. Temporary refs are removed after the
    consumer exits; cleanup errors are noted on an active consumer failure and
    otherwise propagate. Unresolved dedicated evidence is retained. The
    cancellation callback is forwarded to Git metadata-lock acquisition during
    import and cleanup.
    """
    if not source_chain_verified:
        _verify_source_chain(source, source_ref)
    imported_refs: list[TemporaryImportRef] = []
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
            delete_temporary_import_refs(
                imported_refs,
                cancel_requested,
            )
        except Exception as cleanup_error:
            note_cleanup_failure(
                failure,
                "Workspace temporary import ref cleanup",
                cleanup_error,
            )
        raise
    else:
        delete_temporary_import_refs(
            imported_refs,
            cancel_requested,
        )
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
    imported_refs: list[TemporaryImportRef],
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
    _ensure_source_ref_available(
        source,
        source_ref,
        owner,
        cancel_requested,
        imported_refs,
    )
    _reject_source_tree_mismatch(source, source_ref)


def _ensure_source_ref_available(
    source: WorkspaceSourceSnapshot,
    source_ref: WorktreeSourceRef,
    owner: TemporaryRefOwner | None,
    cancel_requested: Callable[[], bool] | None,
    imported_refs: list[TemporaryImportRef],
) -> None:
    if _source_requires_bundle(source_ref):
        _ensure_bundled_source_available(
            source,
            source_ref,
            owner,
            cancel_requested,
            imported_refs,
        )
        return
    _ensure_project_source_available(source, source_ref)


def _ensure_project_source_available(
    source: WorkspaceSourceSnapshot,
    source_ref: WorktreeSourceRef,
) -> None:
    if not _source_commit_exists(source, source_ref.source_commit):
        raise RuntimeError(
            "Workspace lineage source commit is unavailable and no bundle "
            "descriptor was recorded."
        )


def _ensure_bundled_source_available(
    source: WorkspaceSourceSnapshot,
    source_ref: WorktreeSourceRef,
    owner: TemporaryRefOwner | None,
    cancel_requested: Callable[[], bool] | None,
    imported_refs: list[TemporaryImportRef],
) -> None:
    bundle_path = _verify_source_bundle_descriptor(
        git(Path(source.git_top_level)),
        source_ref,
    )
    imported_refs.append(
        import_source_bundle(
            source,
            source_ref,
            bundle_path,
            owner,
            cancel_requested,
        )
    )
    if not _source_commit_exists(source, source_ref.source_commit):
        raise RuntimeError(
            "Workspace lineage source bundle import did not provide the expected "
            "commit."
        )


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
    bundle_path = _require_source_bundle_path(source_ref)
    _require_source_bundle_file(bundle_path)
    bundle_digest = _require_source_bundle_digest(source_ref)
    _verify_source_bundle_digest(bundle_path, bundle_digest)
    bundle_size = _require_source_bundle_size(source_ref)
    _verify_source_bundle_size(bundle_path, bundle_size)
    bundle_ref = _require_source_bundle_ref(source_ref)
    _verify_source_bundle_git_identity(
        command,
        bundle_path,
        bundle_ref,
        source_ref.source_commit,
    )
    return bundle_path


def _require_source_bundle_path(source_ref: WorktreeSourceRef) -> Path:
    if source_ref.bundle_path is None:
        raise RuntimeError("Workspace lineage bundle path is missing.")
    return source_ref.bundle_path


def _require_source_bundle_file(bundle_path: Path) -> None:
    if not bundle_path.is_file():
        raise RuntimeError(
            f"Workspace lineage bundle is missing: {bundle_path.as_posix()}."
        )


def _require_source_bundle_digest(source_ref: WorktreeSourceRef) -> str:
    if source_ref.bundle_sha256 is None:
        raise RuntimeError("Workspace lineage bundle digest is missing.")
    return source_ref.bundle_sha256


def _verify_source_bundle_digest(bundle_path: Path, expected_digest: str) -> None:
    digest = sha256_file(bundle_path)
    if digest != expected_digest:
        raise RuntimeError("Workspace lineage bundle digest mismatch.")


def _require_source_bundle_size(source_ref: WorktreeSourceRef) -> int:
    if source_ref.bundle_size_bytes is None:
        raise RuntimeError("Workspace lineage bundle size is missing.")
    return source_ref.bundle_size_bytes


def _verify_source_bundle_size(bundle_path: Path, expected_size: int) -> None:
    if bundle_path.stat().st_size != expected_size:
        raise RuntimeError("Workspace lineage bundle size mismatch.")


def _require_source_bundle_ref(source_ref: WorktreeSourceRef) -> str:
    if source_ref.bundle_ref is None:
        raise RuntimeError("Workspace lineage bundle ref is missing.")
    return source_ref.bundle_ref


def _verify_source_bundle_git_identity(
    command: GitCommand,
    bundle_path: Path,
    bundle_ref: str,
    source_commit: str,
) -> None:
    command.run("bundle", "verify", bundle_path.as_posix())
    _reject_bundle_ref_mismatch(command, bundle_path, bundle_ref, source_commit)


def _reject_bundle_ref_mismatch(
    command: GitCommand,
    bundle_path: Path,
    bundle_ref: str,
    source_commit: str,
) -> None:
    listed = command.text(
        "bundle",
        "list-heads",
        bundle_path.as_posix(),
        bundle_ref,
    )
    lines = listed.splitlines()
    if len(lines) != 1:
        raise RuntimeError("Workspace lineage bundle ref mismatch.")
    object_id, separator, ref_name = lines[0].partition(" ")
    if separator == " " and object_id == source_commit and ref_name == bundle_ref:
        return
    raise RuntimeError("Workspace lineage bundle ref mismatch.")
