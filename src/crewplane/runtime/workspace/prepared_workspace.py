from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from crewplane.architecture.contracts import InvocationContext

from .locks import git_metadata_lock
from .snapshot import (
    WorkspaceSnapshotPolicy,
    remove_workspace_path,
)
from .snapshot_reporting import snapshot_success_outcome
from .state import read_workspace_state, require_workspace_state_identity
from .terminalization import (
    WorkspaceDiagnosticLevel,
    publish_terminal_workspace_state,
    publish_workspace_cleanup_result,
    workspace_diagnostic,
    workspace_mutators_are_drained,
)
from .worktree import (
    WorktreeCaptureRequest,
    capture_worktree_result,
    inspect_disposable_worktree,
    remove_worktree_workspace,
)
from .worktree.cache import ReusableWorktreeCheckout, WorktreeReuseCache
from .worktree.descriptors import bundle_descriptor, lineage_result_descriptor
from .worktree.head import advance_detached_head_for_reuse
from .worktree.lineage import cleanup_result_refs_after_failure
from .worktree.types import WorktreeCaptureResult


@dataclass(frozen=True, slots=True)
class _WorkspaceSuccessRequest:
    child_environment_applied: bool | None
    defer_cleanup: bool
    cancel_requested: Callable[[], bool] | None


@dataclass(frozen=True, slots=True)
class _LineageSuccessContext:
    workspace_path: Path
    state_path: Path
    capture: WorktreeCaptureRequest


@dataclass
class PreparedWorkspace:
    """Track a prepared invocation workspace through terminalization and cleanup."""

    cwd: Path
    invocation_context: InvocationContext
    workspace_kind: Literal["project_root", "snapshot", "worktree"] = "project_root"
    workspace_path: Path | None = None
    state_path: Path | None = None
    initial_snapshot_entries: dict[str, str] | None = None
    cleanup_on_success: bool = True
    lineage_producer: bool = False
    worktree_capture: WorktreeCaptureRequest | None = None
    reuse_cache: WorktreeReuseCache | None = None
    reuse_key: str | None = None
    workspace_state_payload: dict[str, object] | None = None
    snapshot_cancel_requested: Callable[[], bool] | None = None
    _success_cleanup_physically_removed: bool = field(
        default=False,
        init=False,
        repr=False,
        compare=False,
    )

    def mark_succeeded(
        self,
        child_environment_applied: bool | None = None,
        defer_cleanup: bool = False,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        """Record workspace success and perform cleanup unless deferred."""

        state_path = self._validated_success_state_path()
        if state_path is None:
            return
        request = _WorkspaceSuccessRequest(
            child_environment_applied=child_environment_applied,
            defer_cleanup=defer_cleanup,
            cancel_requested=cancel_requested,
        )
        match self.workspace_kind:
            case "worktree":
                self._mark_worktree_succeeded(request)
            case "snapshot":
                self._mark_snapshot_succeeded(request)
            case "project_root":
                raise RuntimeError(
                    "Workspace success requires worktree capture metadata."
                )
            case _:
                raise RuntimeError(
                    f"Unsupported prepared workspace kind: {self.workspace_kind}."
                )

    def _validated_success_state_path(self) -> Path | None:
        if self.workspace_path is None or self.state_path is None:
            if self.workspace_kind != "project_root":
                raise RuntimeError(
                    "Workspace success requires workspace and state paths."
                )
            return None
        if self.workspace_kind == "worktree" and self.worktree_capture is None:
            raise RuntimeError("Workspace success requires worktree capture metadata.")
        if self.workspace_state_payload is not None:
            require_workspace_state_identity(
                self.state_path,
                self.workspace_state_payload,
            )
        if not workspace_mutators_are_drained(self.state_path):
            raise RuntimeError(
                "Workspace process or worker drain is unresolved; result capture "
                "and cleanup were fenced."
            )
        return self.state_path

    def _mark_worktree_succeeded(
        self,
        request: _WorkspaceSuccessRequest,
    ) -> None:
        if self.lineage_producer:
            self._mark_lineage_worktree_succeeded(request)
            return
        self._mark_disposable_worktree_succeeded(request)

    def _mark_snapshot_succeeded(
        self,
        request: _WorkspaceSuccessRequest,
    ) -> None:
        workspace_path, state_path = self._require_success_workspace_paths(
            "Snapshot success"
        )
        if self.workspace_kind != "snapshot":
            raise RuntimeError("Snapshot success requires a snapshot workspace.")
        cancel_requested = request.cancel_requested
        if cancel_requested is None:
            cancel_requested = self.snapshot_cancel_requested
        initial_entries = self.initial_snapshot_entries
        if initial_entries is None:
            initial_entries = {}
        outcome = snapshot_success_outcome(
            workspace_path / "checkout",
            initial_entries,
            WorkspaceSnapshotPolicy(cancel_requested=cancel_requested),
        )
        self._publish_success_before_cleanup(
            state_path,
            list(outcome.diagnostics),
            outcome.result,
            request,
        )
        self._complete_immediate_success_cleanup(request)

    def _mark_disposable_worktree_succeeded(
        self,
        request: _WorkspaceSuccessRequest,
    ) -> None:
        _, state_path = self._require_success_workspace_paths(
            "Disposable worktree success"
        )
        worktree_capture = self._require_worktree_capture("Disposable worktree success")
        if self.lineage_producer:
            raise RuntimeError(
                "Disposable worktree success requires a non-lineage workspace."
            )
        summary = inspect_disposable_worktree(worktree_capture)
        self._publish_success_before_cleanup(
            state_path,
            list(summary.diagnostics),
            {
                "changed_path_count": summary.changed_path_count,
                "final_head": summary.final_head,
                "lineage_produced": False,
            },
            request,
        )
        self._complete_immediate_success_cleanup(request)

    def _publish_success_before_cleanup(
        self,
        state_path: Path,
        diagnostics: list[dict[str, str]],
        result: Mapping[str, object],
        request: _WorkspaceSuccessRequest,
        refs: Mapping[str, object] | None = None,
        bundle: Mapping[str, object] | None = None,
    ) -> None:
        retained_reason = None
        if self.cleanup_on_success and request.defer_cleanup:
            retained_reason = "stage_finalization_pending"
        elif not self.cleanup_on_success:
            retained_reason = "cleanup_on_success_false"
        publish_terminal_workspace_state(
            state_path,
            "succeeded",
            self.cleanup_on_success,
            diagnostics=diagnostics,
            result=result,
            refs=refs,
            bundle=bundle,
            child_environment_applied=request.child_environment_applied,
            retained_reason=retained_reason,
        )
        self._refresh_workspace_state_payload(state_path)

    def _complete_immediate_success_cleanup(
        self,
        request: _WorkspaceSuccessRequest,
    ) -> None:
        if not self.cleanup_on_success or request.defer_cleanup:
            return
        workspace_path, state_path = self._require_success_workspace_paths(
            "Immediate success cleanup"
        )
        self._remove_success_workspace(workspace_path)
        publish_workspace_cleanup_result(state_path, deleted=True)

    def _mark_lineage_worktree_succeeded(
        self,
        request: _WorkspaceSuccessRequest,
    ) -> None:
        context = self._require_lineage_success_context()
        result = self._capture_lineage_result(context, request.cancel_requested)
        self._commit_lineage_success(context, result, request)

    def _capture_lineage_result(
        self,
        context: _LineageSuccessContext,
        cancel_requested: Callable[[], bool] | None,
    ) -> WorktreeCaptureResult:
        try:
            return capture_worktree_result(context.capture, cancel_requested)
        except Exception:
            self._refresh_workspace_state_payload(context.state_path)
            raise

    def _commit_lineage_success(
        self,
        context: _LineageSuccessContext,
        result: WorktreeCaptureResult,
        request: _WorkspaceSuccessRequest,
    ) -> None:
        try:
            self._finalize_lineage_success(context, result, request)
        except Exception as exc:
            cleanup_result_refs_after_failure(
                context.capture,
                exc,
                request.cancel_requested,
            )
            self._refresh_workspace_state_payload(context.state_path)
            raise

    def _require_lineage_success_context(self) -> _LineageSuccessContext:
        capture = self._require_worktree_capture("Lineage success")
        if self.workspace_kind != "worktree":
            raise RuntimeError("Lineage success requires a worktree workspace.")
        if not self.lineage_producer:
            raise RuntimeError("Lineage success requires a lineage workspace.")
        workspace_path, state_path = self._require_success_workspace_paths(
            "Lineage success"
        )
        return _LineageSuccessContext(
            workspace_path=workspace_path,
            state_path=state_path,
            capture=capture,
        )

    def _require_success_workspace_paths(self, context: str) -> tuple[Path, Path]:
        if self.workspace_path is None or self.state_path is None:
            raise RuntimeError(f"{context} requires workspace and state paths.")
        return self.workspace_path, self.state_path

    def _require_worktree_capture(self, context: str) -> WorktreeCaptureRequest:
        if self.worktree_capture is None:
            raise RuntimeError(f"{context} requires worktree capture metadata.")
        return self.worktree_capture

    def cleanup_after_success(self) -> None:
        """Complete idempotent deferred cleanup for a successful workspace."""

        cleanup_paths = self._success_cleanup_paths()
        if cleanup_paths is None:
            return
        workspace_path, state_path = cleanup_paths
        if self.reuse_cache is not None and self.reuse_cache.owns(workspace_path):
            return
        if self._success_cleanup_physically_removed:
            self._verify_removed_workspace_still_absent(workspace_path)
        else:
            self._remove_success_workspace(workspace_path)
            self._success_cleanup_physically_removed = True
        publish_workspace_cleanup_result(state_path, deleted=True)

    def _success_cleanup_paths(self) -> tuple[Path, Path] | None:
        if (
            self.workspace_path is None
            or self.state_path is None
            or not self.cleanup_on_success
        ):
            return None
        return self.workspace_path, self.state_path

    def _verify_removed_workspace_still_absent(self, workspace_path: Path) -> None:
        if workspace_path.exists() or workspace_path.is_symlink():
            raise RuntimeError(
                "Workspace path reappeared after successful cleanup; cleanup state "
                "was retained."
            )

    def _remove_success_workspace(self, workspace_path: Path) -> None:
        if self.worktree_capture is None:
            remove_workspace_path(workspace_path)
            return
        remove_worktree_workspace(
            self.worktree_capture.source,
            workspace_path,
            self.worktree_capture.git_dir,
        )

    def _reusable_checkout(
        self,
        context: _LineageSuccessContext,
        result: WorktreeCaptureResult,
    ) -> ReusableWorktreeCheckout | None:
        if (
            not self.cleanup_on_success
            or self.reuse_cache is None
            or self.reuse_key is None
        ):
            return None
        capture = context.capture
        return ReusableWorktreeCheckout(
            node_id=capture.node_id,
            logical_worktree_name=self.reuse_key,
            workspace_path=context.workspace_path,
            checkout_root=capture.checkout_root,
            cwd=self.cwd,
            git_dir=capture.git_dir,
            source_commit=result.result_commit,
            source_tree=result.result_tree,
            source=capture.source,
            state_path=context.state_path,
            cleanup_on_success=self.cleanup_on_success,
            repository_id=capture.source.repository_id,
            run_key_name=capture.plan.run_key_name,
            reuse_generation=self._reuse_generation(context.state_path),
        )

    def _reuse_generation(self, state_path: Path) -> int:
        workspace = read_workspace_state(state_path).get("workspace")
        generation = (
            workspace.get("reuse_generation") if isinstance(workspace, dict) else None
        )
        if not isinstance(generation, int) or isinstance(generation, bool):
            raise RuntimeError("Reusable workspace lacks a valid reuse generation.")
        return generation

    def _finalize_lineage_success(
        self,
        context: _LineageSuccessContext,
        result: WorktreeCaptureResult,
        request: _WorkspaceSuccessRequest,
    ) -> None:
        cache_entry = self._prepare_reusable_checkout(context, result, request)
        self._publish_lineage_success(context, result, request)
        self._finish_lineage_retention(context, cache_entry, request)

    def _prepare_reusable_checkout(
        self,
        context: _LineageSuccessContext,
        result: WorktreeCaptureResult,
        request: _WorkspaceSuccessRequest,
    ) -> ReusableWorktreeCheckout | None:
        cache_entry = self._reusable_checkout(context, result)
        if cache_entry is not None:
            with git_metadata_lock(
                Path(context.capture.source.common_git_dir),
                request.cancel_requested,
            ):
                advance_detached_head_for_reuse(
                    context.capture.checkout_root,
                    context.capture.source_ref.source_commit,
                    result.result_commit,
                )
        return cache_entry

    def _publish_lineage_success(
        self,
        context: _LineageSuccessContext,
        result: WorktreeCaptureResult,
        request: _WorkspaceSuccessRequest,
    ) -> None:
        self._publish_success_before_cleanup(
            context.state_path,
            [],
            lineage_result_descriptor(result),
            request,
            refs={"candidate": result.candidate_ref, "result": result.result_ref},
            bundle=bundle_descriptor(
                context.capture.plan,
                result,
                context.state_path,
            ),
        )

    def _finish_lineage_retention(
        self,
        context: _LineageSuccessContext,
        cache_entry: ReusableWorktreeCheckout | None,
        request: _WorkspaceSuccessRequest,
    ) -> None:
        if not workspace_mutators_are_drained(context.state_path):
            return
        if self.cleanup_on_success and not request.defer_cleanup:
            self._complete_immediate_success_cleanup(request)
        elif cache_entry is not None and self.reuse_cache is not None:
            self.reuse_cache.store(cache_entry, request.cancel_requested)

    def mark_failed(
        self,
        message: str,
        child_environment_applied: bool | None = None,
    ) -> None:
        self._mark_terminal_state(
            "failed",
            "error",
            "failure",
            message,
            child_environment_applied,
        )

    def mark_cancelled(
        self,
        message: str,
        child_environment_applied: bool | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        self._mark_terminal_state(
            "cancelled",
            "warning",
            "cancelled",
            message,
            child_environment_applied,
            cancel_requested,
        )

    def _mark_terminal_state(
        self,
        status: Literal["failed", "cancelled"],
        diagnostic_level: WorkspaceDiagnosticLevel,
        retention_reason: str,
        message: str,
        child_environment_applied: bool | None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        if self.state_path is None:
            return
        if self.workspace_state_payload is not None:
            require_workspace_state_identity(
                self.state_path,
                self.workspace_state_payload,
            )
        diagnostics = [workspace_diagnostic(diagnostic_level, message)]
        cleanup_intended = self.workspace_path is not None
        publish_terminal_workspace_state(
            self.state_path,
            status,
            cleanup_intended,
            diagnostics=diagnostics,
            child_environment_applied=child_environment_applied,
            retained_reason=retention_reason,
        )
        self._refresh_workspace_state_payload(self.state_path)
        if not cleanup_intended:
            return
        self._cleanup_after_terminal_publication(
            self.state_path,
            retention_reason,
            cancel_requested,
        )

    def _cleanup_after_terminal_publication(
        self,
        state_path: Path,
        retention_reason: str,
        cancel_requested: Callable[[], bool] | None,
    ) -> None:
        if not workspace_mutators_are_drained(state_path):
            return
        if (
            self.workspace_path is not None
            and self.reuse_cache is not None
            and self.reuse_cache.defer_workspace_cleanup(
                self.workspace_path, state_path
            )
        ):
            return
        try:
            self._remove_failed_workspace(cancel_requested)
        except Exception as exc:
            cleanup_diagnostic = workspace_diagnostic(
                "warning",
                f"Workspace cleanup after terminal invocation state failed: {exc}",
            )
            publish_workspace_cleanup_result(
                state_path,
                deleted=False,
                retained_reason=f"{retention_reason}_cleanup_failed",
                diagnostic=cleanup_diagnostic,
            )
            return
        publish_workspace_cleanup_result(state_path, deleted=True)

    def _refresh_workspace_state_payload(self, state_path: Path) -> None:
        if self.workspace_state_payload is None:
            return
        self.workspace_state_payload.clear()
        self.workspace_state_payload.update(read_workspace_state(state_path))

    def _remove_failed_workspace(
        self,
        cancel_requested: Callable[[], bool] | None,
    ) -> None:
        if self.workspace_path is None:
            return
        if self.workspace_kind == "worktree" and self.worktree_capture is not None:
            self._remove_failed_worktree(cancel_requested)
            return
        if self.workspace_kind == "snapshot":
            remove_workspace_path(self.workspace_path)
            return
        raise RuntimeError("Terminal workspace cleanup lacks materialization evidence.")

    def _remove_failed_worktree(
        self,
        cancel_requested: Callable[[], bool] | None,
    ) -> None:
        if self.workspace_path is None or self.worktree_capture is None:
            return
        remove_worktree_workspace(
            self.worktree_capture.source,
            self.workspace_path,
            self.worktree_capture.git_dir,
            cancel_requested,
        )
        if self.reuse_cache is not None:
            self.reuse_cache.discard_workspace(self.workspace_path)
