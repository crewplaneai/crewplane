from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from crewplane.architecture.contracts import InvocationContext

from .locks import git_metadata_lock
from .snapshot import (
    WorkspaceSnapshotLimitError,
    WorkspaceSnapshotPolicy,
    remove_workspace_path,
    snapshot_drift_summary,
    snapshot_entries,
)
from .state import read_workspace_state, require_workspace_state_identity
from .terminalization import (
    publish_terminal_workspace_state,
    publish_workspace_cleanup_result,
    workspace_mutators_are_drained,
)
from .worktree import (
    WorktreeCaptureRequest,
    capture_worktree_result,
    inspect_disposable_worktree,
    remove_worktree_workspace,
)
from .worktree.cache import ReusableWorktreeCheckout, WorktreeReuseCache
from .worktree.descriptors import bundle_descriptor
from .worktree.head import advance_detached_head_for_reuse
from .worktree.lineage import cleanup_result_refs_after_failure
from .worktree.types import WorktreeCaptureResult

WorkspaceDiagnosticLevel = Literal["error", "warning"]


def workspace_diagnostic(
    level: WorkspaceDiagnosticLevel,
    message: str,
) -> dict[str, str]:
    return {"level": level, "message": message}


@dataclass
class PreparedWorkspace:
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
        if self.workspace_path is None or self.state_path is None:
            if self.workspace_kind != "project_root":
                raise RuntimeError(
                    "Workspace success requires workspace and state paths."
                )
            return
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
        if self.worktree_capture is not None:
            if not self.lineage_producer:
                self._mark_disposable_worktree_succeeded(
                    child_environment_applied,
                    defer_cleanup,
                )
                return
            self._mark_lineage_worktree_succeeded(
                child_environment_applied,
                defer_cleanup,
                cancel_requested,
            )
            return
        if self.workspace_kind == "snapshot":
            self._mark_snapshot_succeeded(
                child_environment_applied,
                defer_cleanup,
                cancel_requested,
            )
            return
        raise RuntimeError("Workspace success requires worktree capture metadata.")

    def _mark_snapshot_succeeded(
        self,
        child_environment_applied: bool | None,
        defer_cleanup: bool,
        cancel_requested: Callable[[], bool] | None,
    ) -> None:
        workspace_path, state_path = self._require_success_workspace_paths(
            "Snapshot success"
        )
        if self.workspace_kind != "snapshot":
            raise RuntimeError("Snapshot success requires a snapshot workspace.")
        diagnostics: list[dict[str, str]] = []
        result: dict[str, object] = {"lineage_produced": False}
        try:
            current_entries = snapshot_entries(
                workspace_path / "checkout",
                WorkspaceSnapshotPolicy(
                    cancel_requested=(
                        cancel_requested or self.snapshot_cancel_requested
                    ),
                ),
            )
        except WorkspaceSnapshotLimitError as exc:
            result["drift_scan_complete"] = False
            result["drift_scan_limit_reason"] = str(exc)
            diagnostics.append(
                workspace_diagnostic(
                    "warning",
                    "Snapshot checkout changes were discarded; final drift is "
                    f"unknown because reporting reached a limit: {exc}",
                )
            )
        else:
            summary = snapshot_drift_summary(
                self.initial_snapshot_entries or {},
                current_entries,
            )
            result.update(
                {
                    "drift_scan_complete": True,
                    "snapshot_drift_discarded": bool(summary.changed_path_count),
                    "changed_path_count": summary.changed_path_count,
                    "changed_paths": list(summary.changed_paths),
                    "changed_paths_truncated": summary.changed_paths_truncated,
                }
            )
            if summary.changed_path_count:
                diagnostics.append(
                    workspace_diagnostic(
                        "warning",
                        "Snapshot checkout changes were discarded "
                        f"({summary.changed_path_count} path(s)).",
                    )
                )
        self._publish_success_before_cleanup(
            state_path,
            diagnostics,
            result,
            child_environment_applied,
            defer_cleanup,
        )
        if self.cleanup_on_success and not defer_cleanup:
            remove_workspace_path(workspace_path)
            publish_workspace_cleanup_result(state_path, deleted=True)

    def _mark_disposable_worktree_succeeded(
        self,
        child_environment_applied: bool | None,
        defer_cleanup: bool,
    ) -> None:
        workspace_path, state_path = self._require_success_workspace_paths(
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
            child_environment_applied,
            defer_cleanup,
        )
        if self.cleanup_on_success and not defer_cleanup:
            remove_worktree_workspace(
                worktree_capture.source, workspace_path, worktree_capture.git_dir
            )
            publish_workspace_cleanup_result(state_path, deleted=True)

    def _publish_success_before_cleanup(
        self,
        state_path: Path,
        diagnostics: list[dict[str, str]],
        result: Mapping[str, object],
        child_environment_applied: bool | None,
        defer_cleanup: bool,
        refs: Mapping[str, object] | None = None,
        bundle: Mapping[str, object] | None = None,
    ) -> None:
        retained_reason = None
        if self.cleanup_on_success and defer_cleanup:
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
            child_environment_applied=child_environment_applied,
            retained_reason=retained_reason,
        )
        self._refresh_workspace_state_payload(state_path)

    def _mark_lineage_worktree_succeeded(
        self,
        child_environment_applied: bool | None,
        defer_cleanup: bool,
        cancel_requested: Callable[[], bool] | None,
    ) -> None:
        worktree_capture = self._require_worktree_capture("Lineage success")
        if self.workspace_kind != "worktree":
            raise RuntimeError("Lineage success requires a worktree workspace.")
        if not self.lineage_producer:
            raise RuntimeError("Lineage success requires a lineage workspace.")
        try:
            result = capture_worktree_result(worktree_capture, cancel_requested)
        except Exception:
            self._refresh_workspace_state_payload(worktree_capture.state_path)
            raise
        try:
            self._record_lineage_success(
                result,
                child_environment_applied,
                defer_cleanup,
                cancel_requested,
            )
        except Exception as exc:
            cleanup_result_refs_after_failure(
                worktree_capture,
                exc,
                cancel_requested,
            )
            self._refresh_workspace_state_payload(worktree_capture.state_path)
            raise

    def _require_success_workspace_paths(self, context: str) -> tuple[Path, Path]:
        if self.workspace_path is None or self.state_path is None:
            raise RuntimeError(f"{context} requires workspace and state paths.")
        return self.workspace_path, self.state_path

    def _require_worktree_capture(self, context: str) -> WorktreeCaptureRequest:
        if self.worktree_capture is None:
            raise RuntimeError(f"{context} requires worktree capture metadata.")
        return self.worktree_capture

    def cleanup_after_success(self) -> None:
        if (
            self.workspace_path is None
            or self.state_path is None
            or not self.cleanup_on_success
        ):
            return
        if self.reuse_cache is not None and self.reuse_cache.owns(self.workspace_path):
            return
        if self._success_cleanup_physically_removed:
            if self.workspace_path.exists() or self.workspace_path.is_symlink():
                raise RuntimeError(
                    "Workspace path reappeared after successful cleanup; cleanup "
                    "state was retained."
                )
        else:
            if self.worktree_capture is not None:
                remove_worktree_workspace(
                    self.worktree_capture.source,
                    self.workspace_path,
                    self.worktree_capture.git_dir,
                )
            else:
                remove_workspace_path(self.workspace_path)
            self._success_cleanup_physically_removed = True
        publish_workspace_cleanup_result(self.state_path, deleted=True)

    def _reusable_checkout(
        self,
        source_commit: str,
        source_tree: str,
    ) -> ReusableWorktreeCheckout | None:
        if (
            not self.cleanup_on_success
            or self.reuse_cache is None
            or self.reuse_key is None
            or self.workspace_path is None
            or self.state_path is None
            or self.worktree_capture is None
        ):
            return None
        return ReusableWorktreeCheckout(
            node_id=self.worktree_capture.node_id,
            logical_worktree_name=self.reuse_key,
            workspace_path=self.workspace_path,
            checkout_root=self.worktree_capture.checkout_root,
            cwd=self.cwd,
            git_dir=self.worktree_capture.git_dir,
            source_commit=source_commit,
            source_tree=source_tree,
            source=self.worktree_capture.source,
            state_path=self.state_path,
            cleanup_on_success=self.cleanup_on_success,
            repository_id=self.worktree_capture.source.repository_id,
            run_key_name=self.worktree_capture.plan.run_key_name,
            reuse_generation=self._reuse_generation(),
        )

    def _reuse_generation(self) -> int:
        if self.state_path is None:
            raise RuntimeError("Reusable workspace requires state evidence.")
        workspace = read_workspace_state(self.state_path).get("workspace")
        generation = (
            workspace.get("reuse_generation") if isinstance(workspace, dict) else None
        )
        if not isinstance(generation, int) or isinstance(generation, bool):
            raise RuntimeError("Reusable workspace lacks a valid reuse generation.")
        return generation

    def _record_lineage_success(
        self,
        result: WorktreeCaptureResult,
        child_environment_applied: bool | None,
        defer_cleanup: bool,
        cancel_requested: Callable[[], bool] | None,
    ) -> None:
        if (
            self.worktree_capture is None
            or self.workspace_path is None
            or self.state_path is None
        ):
            raise RuntimeError("Workspace lineage success requires capture metadata.")
        cache_entry = self._reusable_checkout(result.result_commit, result.result_tree)
        if cache_entry is not None:
            with git_metadata_lock(
                Path(self.worktree_capture.source.common_git_dir),
                cancel_requested,
            ):
                advance_detached_head_for_reuse(
                    self.worktree_capture.checkout_root,
                    self.worktree_capture.source_ref.source_commit,
                    result.result_commit,
                )
        self._publish_success_before_cleanup(
            self.state_path,
            [],
            {
                "candidate_commit": result.candidate_commit,
                "result_commit": result.result_commit,
                "candidate_tree": result.candidate_tree,
                "result_tree": result.result_tree,
                "changed_path_count": result.changed_path_count,
                "empty_result": result.changed_path_count == 0,
                "final_head": result.final_head,
                "unreachable_provider_objects_scanned": False,
            },
            child_environment_applied,
            defer_cleanup,
            refs={"candidate": result.candidate_ref, "result": result.result_ref},
            bundle=bundle_descriptor(
                self.worktree_capture.plan,
                result,
                self.worktree_capture.state_path,
            ),
        )
        if not workspace_mutators_are_drained(self.state_path):
            return
        if self.cleanup_on_success and not defer_cleanup:
            remove_worktree_workspace(
                self.worktree_capture.source,
                self.workspace_path,
                self.worktree_capture.git_dir,
            )
            publish_workspace_cleanup_result(self.state_path, deleted=True)
        elif cache_entry is not None and self.reuse_cache is not None:
            self.reuse_cache.store(cache_entry, cancel_requested)

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
        if not cleanup_intended or not workspace_mutators_are_drained(self.state_path):
            return
        try:
            self._remove_failed_workspace(cancel_requested)
        except Exception as exc:
            cleanup_diagnostic = workspace_diagnostic(
                "warning",
                f"Workspace cleanup after terminal invocation state failed: {exc}",
            )
            publish_workspace_cleanup_result(
                self.state_path,
                deleted=False,
                retained_reason=f"{retention_reason}_cleanup_failed",
                diagnostic=cleanup_diagnostic,
            )
            return
        publish_workspace_cleanup_result(self.state_path, deleted=True)

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
        if self.reuse_cache is not None:
            updated_paths = self.reuse_cache.cleanup_workspace(
                self.workspace_path,
                cancel_requested,
                state_path=self.state_path,
            )
            if updated_paths:
                return
        remove_worktree_workspace(
            self.worktree_capture.source,
            self.workspace_path,
            self.worktree_capture.git_dir,
            cancel_requested,
        )
        if self.reuse_cache is not None:
            self.reuse_cache.discard_workspace(self.workspace_path)
