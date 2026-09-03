from __future__ import annotations

import stat
import subprocess
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from crewplane.core.file_hashing import sha256_file
from crewplane.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSourceSnapshot,
)

from ..cleanup_notes import note_cleanup_failure
from ..git import git, git_error
from ..locks import git_metadata_lock
from ..snapshot import (
    ensure_owner_private_dir,
    remove_workspace_path,
    workspace_run_root,
)
from .cleanup import registered_worktree_paths
from .commit import commit_message, commit_tree
from .head import (
    detach_attached_head_for_disposal,
    prove_detached_head,
    reject_attached_head_after_safe_detachment,
)
from .inspection import (
    WorktreeDriftSummary,
    changed_paths,
    changed_tree_paths,
    inspect_disposable_checkout,
    reject_byte_transforming_attributes,
    reject_gitattributes_drift,
    reject_gitignore_drift,
)
from .lineage import (
    cleanup_result_refs_after_failure,
    ensure_source_commit_available,
    export_bundle,
    update_result_refs,
)
from .policy import (
    active_git_dir,
    reject_common_git_policy_drift,
    reject_worktree_git_policy_drift,
)
from .protected_refs import (
    ProtectedRefSnapshot,
    protected_ref_snapshot_for_source,
    reject_protected_ref_drift,
)
from .refs import safe_file_component
from .result_validation import validate_result_tree
from .temporary_refs import TemporaryRefOwner
from .types import (
    WorktreeCaptureRequest,
    WorktreeCaptureResult,
    WorktreeProvisioningClaim,
    WorktreeSourceRef,
    WorktreeWorkspace,
)


def create_worktree_workspace(
    plan: PreflightExecutionPlan,
    slug: str,
    source: WorkspaceSourceSnapshot,
    source_ref: WorktreeSourceRef,
    protected_ref_scopes: tuple[str, ...] | None = None,
    workspace_family: Literal["workspaces", "review-workspaces"] = "workspaces",
    parent_slug: str | None = None,
    state_path: Path | None = None,
    cancel_requested: Callable[[], bool] | None = None,
    source_chain_verified: bool = False,
    record_fresh_claim: Callable[[WorktreeProvisioningClaim, int], None] | None = None,
) -> WorktreeWorkspace:
    run_root = workspace_run_root(plan, source, workspace_family)
    if parent_slug is not None:
        run_root = run_root / safe_file_component(parent_slug)
        ensure_owner_private_dir(run_root)
    workspace_path = run_root / slug
    if workspace_path.exists() or workspace_path.is_symlink():
        raise RuntimeError(
            f"Workspace path already exists: {workspace_path.as_posix()}"
        )
    workspace_path.mkdir(mode=0o700)
    workspace_path.chmod(0o700)
    checkout_root = workspace_path / "checkout"
    try:
        with (
            ensure_source_commit_available(
                source,
                source_ref,
                TemporaryRefOwner(state_path) if state_path is not None else None,
                cancel_requested,
                source_chain_verified,
            ),
            git_metadata_lock(Path(source.common_git_dir), cancel_requested),
        ):
            lock_mode = _add_locked_detached_worktree(
                source,
                checkout_root,
                source_ref.source_commit,
                _worktree_lock_reason(plan),
            )
        cwd = checkout_root / source.project_root_relative_path
        if source.project_root_relative_path == ".":
            cwd = checkout_root
        git_dir = active_git_dir(checkout_root)
        if record_fresh_claim is not None:
            record_fresh_claim(
                WorktreeProvisioningClaim(
                    workspace_path=workspace_path,
                    checkout_root=checkout_root,
                    cwd=cwd,
                    git_dir=git_dir,
                    lock_mode=lock_mode,
                ),
                1,
            )
        _verify_worktree_ready(source, checkout_root, source_ref)
        protected_refs = _protected_ref_snapshot(
            source,
            protected_ref_scopes,
            source_ref,
        )
    except subprocess.CalledProcessError as exc:
        failure = RuntimeError(
            f"Workspace worktree provisioning failed: {git_error(exc)}"
        )
        if record_fresh_claim is None:
            _remove_worktree_after_provisioning_failure(
                source,
                workspace_path,
                failure,
                cancel_requested,
            )
        raise failure from exc
    except Exception as exc:
        if record_fresh_claim is None:
            _remove_worktree_after_provisioning_failure(
                source,
                workspace_path,
                exc,
                cancel_requested,
            )
        raise
    return WorktreeWorkspace(
        workspace_path=workspace_path,
        checkout_root=checkout_root,
        cwd=cwd,
        git_dir=git_dir,
        source_ref=source_ref,
        protected_refs=protected_refs,
        lock_mode=lock_mode,
    )


def _remove_worktree_after_provisioning_failure(
    source: WorkspaceSourceSnapshot,
    workspace_path: Path,
    failure: BaseException,
    cancel_requested: Callable[[], bool] | None,
) -> None:
    try:
        if cancel_requested is None:
            remove_worktree_workspace(source, workspace_path)
        else:
            remove_worktree_workspace(
                source,
                workspace_path,
                cancel_requested=cancel_requested,
            )
    except Exception as cleanup_error:
        note_cleanup_failure(
            failure,
            "Workspace cleanup after worktree provisioning failure",
            cleanup_error,
        )


def capture_worktree_result(
    request: WorktreeCaptureRequest,
    cancel_requested: Callable[[], bool] | None = None,
) -> WorktreeCaptureResult:
    _verify_capture_workspace_boundary(request)
    _reject_capture_policy_drift(request)
    with git_metadata_lock(Path(request.source.common_git_dir), cancel_requested):
        reject_attached_head_after_safe_detachment(request.checkout_root)
        head_proof = prove_detached_head(
            request.checkout_root,
            request.source_ref.source_commit,
        )
    final_head = head_proof.commit
    reject_protected_ref_drift(request.checkout_root, request.protected_refs)
    _reject_capture_policy_drift(request)
    reject_worktree_git_policy_drift(request.checkout_root)
    changed_path_records = changed_paths(request.checkout_root)
    reject_gitignore_drift(
        request.checkout_root,
        request.source_ref.source_commit,
        changed_path_records,
    )
    reject_gitattributes_drift(
        request.checkout_root,
        request.source_ref.source_commit,
        changed_path_records,
    )
    with TemporaryDirectory(prefix="crewplane-capture-index-") as index_dir:
        capture_index = Path(index_dir) / "capture.index"
        indexed = git(request.checkout_root, capture_index)
        indexed.run("read-tree", request.source_ref.source_commit)
        _reject_capture_policy_drift(request)
        indexed.run("add", "-A", "--", ".")
        result_tree = indexed.text("write-tree")
        indexed.run("diff-files", "--quiet", "--")
    accepted_changed_paths = changed_tree_paths(
        request.checkout_root,
        request.source_ref.source_tree,
        result_tree,
    )
    reject_byte_transforming_attributes(
        request.checkout_root,
        accepted_changed_paths,
    )
    validate_result_tree(
        request.checkout_root,
        result_tree,
        request.source.project_root_relative_path,
    )
    result_commit = commit_tree(
        request.checkout_root,
        result_tree,
        request.source_ref.source_commit,
        commit_message(request, result_tree),
    )
    candidate_commit = result_commit
    refs = update_result_refs(
        request,
        candidate_commit,
        result_commit,
        cancel_requested,
    )
    try:
        bundle_path = export_bundle(request, refs[1], cancel_requested)
    except Exception as exc:
        cleanup_result_refs_after_failure(request, exc, cancel_requested)
        raise
    bundle_sha256 = sha256_file(bundle_path)
    return WorktreeCaptureResult(
        candidate_commit=candidate_commit,
        result_commit=result_commit,
        candidate_tree=result_tree,
        result_tree=result_tree,
        changed_path_count=len(accepted_changed_paths),
        bundle_path=bundle_path,
        bundle_sha256=bundle_sha256,
        bundle_size_bytes=bundle_path.stat().st_size,
        candidate_ref=refs[0],
        result_ref=refs[1],
        final_head=final_head,
    )


def inspect_disposable_worktree(
    request: WorktreeCaptureRequest,
) -> WorktreeDriftSummary:
    return inspect_disposable_checkout(
        request.checkout_root,
        request.source_ref.source_commit,
        request.protected_refs,
        Path(request.source.git_top_level),
        Path(request.source.common_git_dir),
        request.source.project_root_relative_path,
    )


def remove_worktree_workspace(
    source: WorkspaceSourceSnapshot,
    workspace_path: Path,
    expected_git_dir: Path | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> None:
    if expected_git_dir is not None and (
        workspace_path.is_symlink() or not workspace_path.is_dir()
    ):
        raise RuntimeError(
            "Persisted workspace path is missing or unsafe; cleanup was retained."
        )
    if workspace_path.is_symlink() or not workspace_path.is_dir():
        remove_workspace_path(workspace_path)
        return
    checkout_root = workspace_path / "checkout"
    if expected_git_dir is not None and (
        checkout_root.is_symlink() or not checkout_root.is_dir()
    ):
        raise RuntimeError(
            "Persisted workspace checkout is missing or unsafe; cleanup was retained."
        )
    if checkout_root.is_symlink():
        remove_workspace_path(workspace_path)
        return
    with git_metadata_lock(Path(source.common_git_dir), cancel_requested):
        if _worktree_is_registered(source, checkout_root):
            if not checkout_root.is_dir():
                raise RuntimeError(
                    "Registered workspace checkout is missing; cleanup was retained."
                )
            git_file = _valid_worktree_git_file(checkout_root)
            git_dir = active_git_dir(checkout_root)
            if expected_git_dir is not None and git_dir != expected_git_dir.resolve(
                strict=False
            ):
                raise RuntimeError(
                    "Registered workspace Git dir does not match materialized "
                    "identity; cleanup was retained."
                )
            if not git_dir.is_relative_to(
                Path(source.common_git_dir).resolve(strict=False)
            ):
                raise RuntimeError(
                    "Registered workspace Git dir escapes the common Git dir; "
                    "cleanup was retained."
                )
            _reject_capture_gitdir_mismatch(git_file, git_dir)
            detach_attached_head_for_disposal(checkout_root)
            git(Path(source.git_top_level)).run(
                "worktree",
                "remove",
                "--force",
                "--force",
                checkout_root.as_posix(),
            )
        else:
            git_entry = checkout_root / ".git"
            if (
                expected_git_dir is not None
                or git_entry.exists()
                or git_entry.is_symlink()
            ):
                raise RuntimeError(
                    "Workspace checkout is not registered; cleanup was retained."
                )
    remove_workspace_path(workspace_path)


def _worktree_is_registered(
    source: WorkspaceSourceSnapshot,
    checkout_root: Path,
) -> bool:
    expected = checkout_root.resolve(strict=False)
    return expected in registered_worktree_paths(Path(source.common_git_dir))


def _verify_capture_workspace_boundary(request: WorktreeCaptureRequest) -> None:
    _reject_unsafe_capture_directory(
        request.workspace_path,
        "Workspace capture root",
    )
    if request.checkout_root != request.workspace_path / "checkout":
        raise RuntimeError("Workspace capture checkout path is not under its root.")
    _reject_unsafe_capture_directory(
        request.checkout_root,
        "Workspace capture checkout",
    )
    git_file = _valid_worktree_git_file(request.checkout_root)
    git_dir = active_git_dir(request.checkout_root)
    if git_dir != request.git_dir.resolve(strict=False):
        raise RuntimeError("Workspace capture Git dir changed after materialization.")
    common_git_dir = Path(request.source.common_git_dir).resolve(strict=False)
    if not git_dir.is_relative_to(common_git_dir):
        raise RuntimeError("Workspace capture Git dir escapes the common Git dir.")
    _reject_capture_gitdir_mismatch(git_file, git_dir)
    if not _worktree_is_registered(request.source, request.checkout_root):
        raise RuntimeError("Workspace capture checkout is not registered.")


def _reject_unsafe_capture_directory(path: Path, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise RuntimeError(f"{label} is missing: {path.as_posix()}.") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise RuntimeError(
            f"{label} must be a real directory and not a symlink: {path.as_posix()}."
        )


def _valid_worktree_git_file(checkout_root: Path) -> Path:
    git_file = checkout_root / ".git"
    try:
        mode = git_file.lstat().st_mode
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Workspace capture requires a valid worktree .git file."
        ) from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise RuntimeError("Workspace capture requires a valid worktree .git file.")
    return git_file


def _reject_capture_gitdir_mismatch(git_file: Path, git_dir: Path) -> None:
    marker_target = _worktree_gitdir_marker_target(git_file)
    if marker_target != git_dir:
        raise RuntimeError("Workspace capture .git file does not match Git dir.")
    backlink = _worktree_gitdir_backlink(git_dir)
    if backlink != git_file.resolve(strict=False):
        raise RuntimeError("Workspace capture Git dir does not belong to checkout.")


def _worktree_gitdir_marker_target(git_file: Path) -> Path:
    marker = "gitdir:"
    content = git_file.read_text(encoding="utf-8", errors="replace").strip()
    if not content.startswith(marker):
        raise RuntimeError("Workspace capture found an invalid worktree .git file.")
    raw_path = content[len(marker) :].strip()
    if not raw_path:
        raise RuntimeError("Workspace capture found an empty worktree Git dir.")
    target = Path(raw_path)
    if not target.is_absolute():
        target = git_file.parent / target
    return target.resolve(strict=False)


def _worktree_gitdir_backlink(git_dir: Path) -> Path:
    gitdir_file = git_dir / "gitdir"
    try:
        mode = gitdir_file.lstat().st_mode
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Workspace capture Git dir is missing its checkout pointer."
        ) from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise RuntimeError("Workspace capture Git dir checkout pointer is invalid.")
    raw_path = gitdir_file.read_text(encoding="utf-8", errors="replace").strip()
    if not raw_path:
        raise RuntimeError("Workspace capture Git dir checkout pointer is empty.")
    target = Path(raw_path)
    if not target.is_absolute():
        target = git_dir / target
    return target.resolve(strict=False)


def _verify_worktree_ready(
    source: WorkspaceSourceSnapshot,
    checkout_root: Path,
    source_ref: WorktreeSourceRef,
) -> None:
    command = git(checkout_root)
    head = command.text("rev-parse", "HEAD^{commit}")
    if head != source_ref.source_commit:
        raise RuntimeError("Workspace worktree HEAD does not match source commit.")
    if command.text("rev-parse", "--is-inside-work-tree") != "true":
        raise RuntimeError("Workspace worktree was not registered correctly.")
    git_dir = active_git_dir(checkout_root)
    if not git_dir.is_relative_to(Path(source.common_git_dir)):
        raise RuntimeError("Workspace worktree Git dir escapes the Git common dir.")
    reject_common_git_policy_drift(
        Path(source.git_top_level),
        Path(source.common_git_dir),
    )
    reject_worktree_git_policy_drift(checkout_root)


def _add_locked_detached_worktree(
    source: WorkspaceSourceSnapshot,
    checkout_root: Path,
    source_commit: str,
    reason: str,
) -> str:
    command = git(Path(source.git_top_level))
    try:
        command.run(
            "worktree",
            "add",
            "--detach",
            "--lock",
            "--reason",
            reason,
            checkout_root.as_posix(),
            source_commit,
        )
        return "add_lock_reason"
    except subprocess.CalledProcessError as exc:
        if not _worktree_add_lock_reason_unsupported(exc):
            raise
    command.run(
        "worktree",
        "add",
        "--detach",
        checkout_root.as_posix(),
        source_commit,
    )
    command.run("worktree", "lock", "--reason", reason, checkout_root.as_posix())
    return "lock_after_add"


def _worktree_add_lock_reason_unsupported(
    exc: subprocess.CalledProcessError,
) -> bool:
    if exc.returncode != 129:
        return False
    error = git_error(exc).casefold()
    return "unknown option" in error or "usage: git worktree add" in error


def _worktree_lock_reason(plan: PreflightExecutionPlan) -> str:
    return f"crewplane {plan.run_key_name}"


def _reject_capture_policy_drift(request: WorktreeCaptureRequest) -> None:
    reject_common_git_policy_drift(
        Path(request.source.git_top_level),
        Path(request.source.common_git_dir),
    )


def _protected_ref_snapshot(
    source: WorkspaceSourceSnapshot,
    protected_ref_scopes: tuple[str, ...] | None,
    source_ref: WorktreeSourceRef,
) -> ProtectedRefSnapshot:
    return protected_ref_snapshot_for_source(
        source.git_top_level,
        protected_ref_scopes,
        source_ref,
    )
