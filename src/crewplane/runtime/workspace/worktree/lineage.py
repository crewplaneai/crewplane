from __future__ import annotations

import stat
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

from crewplane.core.file_hashing import sha256_file
from crewplane.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSourceSnapshot,
)

from ..cleanup_notes import note_cleanup_failure
from ..git import GitCommand, git
from ..locks import git_metadata_lock
from .protected_refs import PROTECTED_REF_PREFIX
from .refs import checked_ref, safe_file_component, safe_ref_component
from .types import WorktreeCaptureRequest, WorktreeSourceRef


def ensure_source_commit_available(
    source: WorkspaceSourceSnapshot,
    source_ref: WorktreeSourceRef,
) -> None:
    _ensure_source_commit_available(source, source_ref, set())


def verify_source_commit_available(
    source: WorkspaceSourceSnapshot,
    source_ref: WorktreeSourceRef,
) -> None:
    with TemporaryDirectory(prefix="crewplane-lineage-verify-") as temp_dir:
        git_dir = Path(temp_dir) / "verify.git"
        git(Path(temp_dir)).run(
            "init",
            "--bare",
            f"--object-format={source.object_format}",
            git_dir.as_posix(),
        )
        command = git(git_dir)
        _fetch_commit_for_verification(command, source, source.run_base_commit)
        _verify_source_commit_available(source, command, source_ref, set())


def _ensure_source_commit_available(
    source: WorkspaceSourceSnapshot,
    source_ref: WorktreeSourceRef,
    active_commits: set[str],
) -> None:
    if source_ref.source_commit in active_commits:
        raise RuntimeError("Workspace lineage source chain contains a cycle.")
    active_commits.add(source_ref.source_commit)
    for upstream in source_ref.upstream_sources:
        _ensure_source_commit_available(source, upstream, active_commits)
    active_commits.remove(source_ref.source_commit)

    verified_bundle_path: Path | None = None
    if _source_requires_bundle(source_ref):
        verified_bundle_path = _verify_source_bundle_descriptor(
            git(Path(source.git_top_level)),
            source_ref,
        )
    if _source_commit_exists(source, source_ref.source_commit):
        _reject_source_tree_mismatch(source, source_ref)
        return
    if source_ref.bundle_path is None:
        raise RuntimeError(
            "Workspace lineage source commit is unavailable and no bundle "
            "descriptor was recorded."
        )
    _import_source_bundle(source, source_ref, verified_bundle_path)
    if not _source_commit_exists(source, source_ref.source_commit):
        raise RuntimeError(
            "Workspace lineage source bundle import did not provide the expected "
            "commit."
        )
    _reject_source_tree_mismatch(source, source_ref)


def _verify_source_commit_available(
    source: WorkspaceSourceSnapshot,
    command: GitCommand,
    source_ref: WorktreeSourceRef,
    active_commits: set[str],
) -> None:
    if source_ref.source_commit in active_commits:
        raise RuntimeError("Workspace lineage source chain contains a cycle.")
    active_commits.add(source_ref.source_commit)
    for upstream in source_ref.upstream_sources:
        _verify_source_commit_available(source, command, upstream, active_commits)
    active_commits.remove(source_ref.source_commit)

    if _source_requires_bundle(source_ref):
        bundle_path = _verify_source_bundle_descriptor(command, source_ref)
        _fetch_bundle_for_verification(command, bundle_path, source_ref)
    elif not _command_commit_exists(command, source_ref.source_commit):
        _fetch_commit_for_verification(command, source, source_ref.source_commit)
    if not _command_commit_exists(command, source_ref.source_commit):
        raise RuntimeError(
            "Workspace lineage source bundle import did not provide the expected "
            "commit."
        )
    _reject_source_tree_mismatch_with_command(command, source_ref)


def update_result_refs(
    request: WorktreeCaptureRequest,
    candidate_commit: str,
    result_commit: str,
) -> tuple[str, str]:
    base = _result_ref_base(request.plan.run_key_name, request.node_id, request.slug)
    candidate_ref = checked_ref(request.checkout_root, f"{base}/candidate")
    result_ref = checked_ref(request.checkout_root, f"{base}/result")
    with git_metadata_lock(Path(request.source.common_git_dir)):
        command = git(request.checkout_root)
        updated_refs: list[str] = []
        try:
            command.run("update-ref", candidate_ref, candidate_commit)
            updated_refs.append(candidate_ref)
            command.run("update-ref", result_ref, result_commit)
            updated_refs.append(result_ref)
        except Exception as exc:
            try:
                _delete_refs(command, tuple(updated_refs))
            except Exception as cleanup_error:
                note_cleanup_failure(
                    exc,
                    "Workspace result ref cleanup after partial ref update",
                    cleanup_error,
                )
            raise
    return candidate_ref, result_ref


def delete_result_refs(
    request: WorktreeCaptureRequest,
    refs: tuple[str, str],
) -> None:
    with git_metadata_lock(Path(request.source.common_git_dir)):
        _delete_refs(git(request.checkout_root), refs)


def cleanup_result_refs_after_failure(
    request: WorktreeCaptureRequest,
    refs: tuple[str, str],
    failure: BaseException,
) -> None:
    try:
        delete_result_refs(request, refs)
    except Exception as cleanup_error:
        note_cleanup_failure(
            failure,
            "Workspace result ref cleanup after capture failure",
            cleanup_error,
        )


def export_bundle(request: WorktreeCaptureRequest, result_ref: str) -> Path:
    bundle_dir = request.state_path.parent / "workspace-bundles"
    _ensure_safe_bundle_dir(request.state_path.parent, bundle_dir)
    bundle_dir.chmod(0o700)
    bundle_path = bundle_dir / f"{safe_file_component(request.slug)}.bundle"
    temp_bundle_path = _temporary_bundle_path(bundle_dir, request.slug)
    try:
        with git_metadata_lock(Path(request.source.common_git_dir)):
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


def _delete_refs(command: GitCommand, refs: tuple[str, ...]) -> None:
    for ref in refs:
        command.run("update-ref", "-d", ref)


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
    del plan, source_ref, node_id, slug
    return (PROTECTED_REF_PREFIX,)


def _source_commit_exists(source: WorkspaceSourceSnapshot, commit: str) -> bool:
    try:
        git(Path(source.git_top_level)).run("cat-file", "-e", f"{commit}^{{commit}}")
    except subprocess.CalledProcessError:
        return False
    return True


def _command_commit_exists(command: GitCommand, commit: str) -> bool:
    try:
        command.run("cat-file", "-e", f"{commit}^{{commit}}")
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


def _reject_source_tree_mismatch_with_command(
    command: GitCommand,
    source_ref: WorktreeSourceRef,
) -> None:
    actual_tree = command.text(
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
) -> None:
    command = git(Path(source.git_top_level))
    bundle_path = verified_bundle_path or _verify_source_bundle_descriptor(
        command,
        source_ref,
    )
    import_ref = checked_ref(
        Path(source.git_top_level),
        _import_ref_for_source_commit(source_ref.source_commit),
    )
    with git_metadata_lock(Path(source.common_git_dir)):
        command.run(
            "fetch",
            bundle_path.as_posix(),
            f"{source_ref.bundle_ref}:{import_ref}",
        )


def _fetch_bundle_for_verification(
    command: GitCommand,
    bundle_path: Path,
    source_ref: WorktreeSourceRef,
) -> None:
    import_ref = _import_ref_for_source_commit(source_ref.source_commit)
    command.run(
        "fetch",
        bundle_path.as_posix(),
        f"{source_ref.bundle_ref}:{import_ref}",
    )


def _fetch_commit_for_verification(
    command: GitCommand,
    source: WorkspaceSourceSnapshot,
    commit: str,
) -> None:
    command.run(
        "fetch",
        Path(source.git_top_level).as_posix(),
        f"{commit}:{_import_ref_for_source_commit(commit)}",
    )


def _result_ref_base(run_key_name: str, node_id: str, slug: str) -> str:
    return (
        "refs/crewplane/runs/"
        f"{safe_ref_component(run_key_name)}/"
        f"{safe_ref_component(node_id)}/"
        f"{safe_ref_component(slug)}"
    )


def _import_ref_for_source_commit(source_commit: str) -> str:
    return f"refs/crewplane/imported/{safe_ref_component(source_commit[:24])}"
