from __future__ import annotations

import os
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
from .checkout_identity import (
    require_regular_worktree_git_file,
    verify_capture_layout,
    verify_worktree_git_metadata_identity,
    worktree_is_registered,
)
from .checkout_placement import allocate_worktree_workspace, worktree_project_cwd
from .commit import commit_message, commit_tree
from .head import (
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
from .removal import (
    remove_claimed_worktree_workspace,
    remove_unclaimed_worktree_workspace,
)
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
    workspace_path, checkout_root = allocate_worktree_workspace(
        plan,
        slug,
        source,
        workspace_family,
        parent_slug,
    )
    try:
        lock_mode = _provision_worktree_checkout(
            plan,
            source,
            source_ref,
            checkout_root,
            state_path,
            cancel_requested,
            source_chain_verified,
        )
        cwd = worktree_project_cwd(source, checkout_root)
        git_dir = active_git_dir(checkout_root)
        claim = WorktreeProvisioningClaim(
            workspace_path=workspace_path,
            checkout_root=checkout_root,
            cwd=cwd,
            git_dir=git_dir,
            lock_mode=lock_mode,
        )
        _record_worktree_claim(
            record_fresh_claim,
            claim,
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


def _provision_worktree_checkout(
    plan: PreflightExecutionPlan,
    source: WorkspaceSourceSnapshot,
    source_ref: WorktreeSourceRef,
    checkout_root: Path,
    state_path: Path | None,
    cancel_requested: Callable[[], bool] | None,
    source_chain_verified: bool,
) -> str:
    ref_owner = TemporaryRefOwner(state_path) if state_path is not None else None
    with (
        ensure_source_commit_available(
            source,
            source_ref,
            ref_owner,
            cancel_requested,
            source_chain_verified,
        ),
        git_metadata_lock(Path(source.common_git_dir), cancel_requested),
    ):
        return _add_locked_detached_worktree(
            source,
            checkout_root,
            source_ref.source_commit,
            _worktree_lock_reason(plan),
        )


def _record_worktree_claim(
    record_fresh_claim: Callable[[WorktreeProvisioningClaim, int], None] | None,
    claim: WorktreeProvisioningClaim,
) -> None:
    if record_fresh_claim is None:
        return
    record_fresh_claim(claim, 1)


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
    final_head = _validate_capture_source_state(
        request,
        cancel_requested,
    )
    result_tree = _stage_capture_tree(request)
    accepted_changed_paths = _validate_capture_result_tree(request, result_tree)
    result_commit = _create_capture_commit(request, result_tree)
    refs, bundle_path = _publish_capture_result(
        request,
        result_commit,
        cancel_requested,
    )
    bundle_sha256 = sha256_file(bundle_path)
    return WorktreeCaptureResult(
        candidate_commit=result_commit,
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


def _validate_capture_source_state(
    request: WorktreeCaptureRequest,
    cancel_requested: Callable[[], bool] | None,
) -> str:
    _verify_capture_workspace_boundary(request)
    _reject_capture_policy_drift(request)
    with git_metadata_lock(Path(request.source.common_git_dir), cancel_requested):
        reject_attached_head_after_safe_detachment(request.checkout_root)
        head_proof = prove_detached_head(
            request.checkout_root,
            request.source_ref.source_commit,
        )
    reject_protected_ref_drift(request.checkout_root, request.protected_refs)
    _reject_capture_policy_drift(request)
    reject_worktree_git_policy_drift(request.checkout_root)
    changed_path_records = changed_paths(request.checkout_root)
    reject_gitignore_drift(
        request.checkout_root,
        request.source_ref.source_commit,
    )
    reject_gitattributes_drift(
        request.checkout_root,
        request.source_ref.source_commit,
        changed_path_records,
    )
    return head_proof.commit


def _stage_capture_tree(request: WorktreeCaptureRequest) -> str:
    with TemporaryDirectory(prefix="crewplane-capture-index-") as index_dir:
        capture_index = Path(index_dir) / "capture.index"
        indexed = git(request.checkout_root, capture_index)
        indexed.run("read-tree", request.source_ref.source_commit)
        _reject_capture_policy_drift(request)
        indexed.run("add", "-A", "--", ".")
        result_tree = indexed.text("write-tree")
        indexed.run("diff-files", "--quiet", "--")
    return result_tree


def _validate_capture_result_tree(
    request: WorktreeCaptureRequest,
    result_tree: str,
) -> tuple[str, ...]:
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
    return accepted_changed_paths


def _create_capture_commit(
    request: WorktreeCaptureRequest,
    result_tree: str,
) -> str:
    return commit_tree(
        request.checkout_root,
        result_tree,
        request.source_ref.source_commit,
        commit_message(request, result_tree),
    )


def _publish_capture_result(
    request: WorktreeCaptureRequest,
    result_commit: str,
    cancel_requested: Callable[[], bool] | None,
) -> tuple[tuple[str, str], Path]:
    refs = update_result_refs(
        request,
        result_commit,
        result_commit,
        cancel_requested,
    )
    try:
        bundle_path = export_bundle(request, refs[1], cancel_requested)
    except Exception as exc:
        cleanup_result_refs_after_failure(request, exc, cancel_requested)
        raise
    return refs, bundle_path


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
    if expected_git_dir is None:
        remove_unclaimed_worktree_workspace(
            source,
            workspace_path,
            cancel_requested,
        )
        return
    remove_claimed_worktree_workspace(
        source,
        workspace_path,
        expected_git_dir,
        cancel_requested,
    )


def _verify_capture_workspace_boundary(request: WorktreeCaptureRequest) -> None:
    verify_capture_layout(request)
    git_file = require_regular_worktree_git_file(request.checkout_root)
    git_dir = active_git_dir(request.checkout_root)
    _verify_capture_git_dir_descriptor(request, git_dir)
    verify_worktree_git_metadata_identity(git_file, git_dir)
    if not worktree_is_registered(request.source, request.checkout_root):
        raise RuntimeError("Workspace capture checkout is not registered.")


def _verify_capture_git_dir_descriptor(
    request: WorktreeCaptureRequest,
    git_dir: Path,
) -> None:
    if git_dir != request.git_dir.resolve(strict=False):
        raise RuntimeError("Workspace capture Git dir changed after materialization.")
    common_git_dir = Path(request.source.common_git_dir).resolve(strict=False)
    if not git_dir.is_relative_to(common_git_dir):
        raise RuntimeError("Workspace capture Git dir escapes the common Git dir.")


def _verify_worktree_ready(
    source: WorkspaceSourceSnapshot,
    checkout_root: Path,
    source_ref: WorktreeSourceRef,
) -> None:
    _verify_worktree_source_identity(source, checkout_root, source_ref)
    _verify_worktree_policy(source, checkout_root)


def _verify_worktree_source_identity(
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


def _verify_worktree_policy(
    source: WorkspaceSourceSnapshot,
    checkout_root: Path,
) -> None:
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
            "-c",
            f"core.hooksPath={os.devnull}",
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
        "-c",
        f"core.hooksPath={os.devnull}",
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
