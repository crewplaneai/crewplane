from __future__ import annotations

import stat
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from orchestrator_cli.core.file_hashing import sha256_file
from orchestrator_cli.core.preflight.models import (
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
from .commit import commit_message, commit_tree
from .inspection import (
    WorktreeDriftSummary,
    changed_paths,
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
    protected_ref_snapshot,
    protected_ref_snapshot_for_scopes,
    reject_protected_ref_drift,
)
from .refs import checked_ref, safe_file_component
from .result_validation import validate_result_tree
from .types import (
    WorktreeCaptureRequest,
    WorktreeCaptureResult,
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
        ensure_source_commit_available(source, source_ref)
        with git_metadata_lock(Path(source.common_git_dir)):
            lock_mode = _add_locked_detached_worktree(
                source,
                checkout_root,
                source_ref.source_commit,
                _worktree_lock_reason(plan),
            )
        cwd = checkout_root / source.project_root_relative_path
        if source.project_root_relative_path == ".":
            cwd = checkout_root
        _verify_worktree_ready(source, checkout_root, source_ref)
        git_dir = active_git_dir(checkout_root)
        protected_refs = _protected_ref_snapshot(
            source,
            protected_ref_scopes,
        )
    except subprocess.CalledProcessError as exc:
        failure = RuntimeError(
            f"Workspace worktree provisioning failed: {git_error(exc)}"
        )
        _remove_worktree_after_provisioning_failure(source, workspace_path, failure)
        raise failure from exc
    except Exception as exc:
        _remove_worktree_after_provisioning_failure(source, workspace_path, exc)
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
) -> None:
    try:
        remove_worktree_workspace(source, workspace_path)
    except Exception as cleanup_error:
        note_cleanup_failure(
            failure,
            "Workspace cleanup after worktree provisioning failure",
            cleanup_error,
        )


def capture_worktree_result(
    request: WorktreeCaptureRequest,
) -> WorktreeCaptureResult:
    _verify_capture_workspace_boundary(request)
    _reject_capture_policy_drift(request)
    command = git(request.checkout_root)
    final_head = command.text("rev-parse", "HEAD^{commit}")
    if final_head != request.source_ref.source_commit:
        raise RuntimeError(
            "Workspace provider moved HEAD; mutable workspace capture requires "
            "the final HEAD to remain at the invocation source commit."
        )
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
    reject_byte_transforming_attributes(request.checkout_root, changed_path_records)
    with TemporaryDirectory(prefix="orchestrator-cli-capture-index-") as index_dir:
        capture_index = Path(index_dir) / "capture.index"
        indexed = git(request.checkout_root, capture_index)
        indexed.run("read-tree", request.source_ref.source_commit)
        _reject_capture_policy_drift(request)
        indexed.run("add", "-A", "--", ".")
        result_tree = indexed.text("write-tree")
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
    refs = update_result_refs(request, candidate_commit, result_commit)
    try:
        bundle_path = export_bundle(request, refs[1])
    except Exception as exc:
        cleanup_result_refs_after_failure(request, refs, exc)
        raise
    bundle_sha256 = sha256_file(bundle_path)
    return WorktreeCaptureResult(
        candidate_commit=candidate_commit,
        result_commit=result_commit,
        candidate_tree=result_tree,
        result_tree=result_tree,
        changed_path_count=len(changed_path_records),
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
) -> None:
    checkout_root = workspace_path / "checkout"
    with git_metadata_lock(Path(source.common_git_dir)):
        if _worktree_is_registered(source, checkout_root):
            git(Path(source.git_top_level)).run(
                "worktree",
                "remove",
                "--force",
                "--force",
                checkout_root.as_posix(),
            )
    remove_workspace_path(workspace_path)


def _worktree_is_registered(
    source: WorkspaceSourceSnapshot,
    checkout_root: Path,
) -> bool:
    expected = checkout_root.absolute()
    records = (
        git(Path(source.git_top_level))
        .text(
            "worktree",
            "list",
            "--porcelain",
        )
        .splitlines()
    )
    return any(
        _registered_worktree_path(record) == expected
        for record in records
        if record.startswith("worktree ")
    )


def _registered_worktree_path(record: str) -> Path:
    return Path(record.removeprefix("worktree ")).absolute()


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
    common_git_dir = Path(request.source.common_git_dir).resolve(strict=False)
    if not git_dir.is_relative_to(common_git_dir):
        raise RuntimeError("Workspace capture Git dir escapes the common Git dir.")
    _reject_capture_gitdir_mismatch(git_file, git_dir)


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
    return f"orchestrator-cli {plan.run_key_name}"


def _reject_capture_policy_drift(request: WorktreeCaptureRequest) -> None:
    reject_common_git_policy_drift(
        Path(request.source.git_top_level),
        Path(request.source.common_git_dir),
    )


def _protected_ref_snapshot(
    source: WorkspaceSourceSnapshot,
    protected_ref_scopes: tuple[str, ...] | None,
) -> ProtectedRefSnapshot:
    repo_root = Path(source.git_top_level)
    if protected_ref_scopes is None:
        return protected_ref_snapshot(repo_root)
    checked_scopes = tuple(
        checked_ref(repo_root, scope) for scope in protected_ref_scopes
    )
    return protected_ref_snapshot_for_scopes(repo_root, checked_scopes)
