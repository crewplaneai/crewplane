from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from orchestrator_cli.architecture.contracts import InvocationContext

from .reuse import ReusableWorktreeCheckout, WorktreeReuseCache
from .snapshot import (
    remove_workspace_path,
    snapshot_drift_summary,
    snapshot_entries,
)
from .state import (
    WorkspaceStateRetention,
    WorkspaceStateUpdateRequest,
    update_workspace_state,
)
from .worktree import (
    WorktreeCaptureRequest,
    capture_worktree_result,
    inspect_disposable_worktree,
    remove_worktree_workspace,
)
from .worktree_descriptors import bundle_descriptor
from .worktree_lineage import cleanup_result_refs_after_failure
from .worktree_types import WorktreeCaptureResult

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

    def mark_succeeded(
        self,
        child_environment_applied: bool | None = None,
        defer_cleanup: bool = False,
    ) -> None:
        if self.workspace_path is None or self.state_path is None:
            if self.workspace_kind != "project_root":
                raise RuntimeError(
                    "Workspace success requires workspace and state paths."
                )
            return
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
            )
            return
        if self.workspace_kind == "snapshot":
            self._mark_snapshot_succeeded(child_environment_applied, defer_cleanup)
            return
        raise RuntimeError("Workspace success requires worktree capture metadata.")

    def _mark_snapshot_succeeded(
        self,
        child_environment_applied: bool | None,
        defer_cleanup: bool,
    ) -> None:
        workspace_path, state_path = self._require_success_workspace_paths(
            "Snapshot success"
        )
        if self.workspace_kind != "snapshot":
            raise RuntimeError("Snapshot success requires a snapshot workspace.")
        current_entries = snapshot_entries(workspace_path / "checkout")
        diagnostics = []
        summary = snapshot_drift_summary(
            self.initial_snapshot_entries or {},
            current_entries,
        )
        result = {
            "lineage_produced": False,
            "snapshot_drift_discarded": bool(summary.changed_path_count),
            "changed_path_count": summary.changed_path_count,
            "changed_paths": list(summary.changed_paths),
            "changed_paths_truncated": summary.changed_paths_truncated,
        }
        if summary.changed_path_count:
            diagnostics.append(
                workspace_diagnostic(
                    "warning",
                    "Snapshot checkout changes were discarded "
                    f"({summary.changed_path_count} path(s)).",
                )
            )
        if self.cleanup_on_success and not defer_cleanup:
            remove_workspace_path(workspace_path)
        update_workspace_state(
            state_path,
            WorkspaceStateUpdateRequest(
                status="succeeded",
                diagnostics=diagnostics,
                retention=self._success_state_retention(defer_cleanup),
                child_environment_applied=child_environment_applied,
                result=result,
                base_payload=self.workspace_state_payload,
            ),
        )

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
        if self.cleanup_on_success and not defer_cleanup:
            remove_worktree_workspace(
                worktree_capture.source,
                workspace_path,
            )
        update_workspace_state(
            state_path,
            WorkspaceStateUpdateRequest(
                status="succeeded",
                diagnostics=list(summary.diagnostics),
                retention=self._success_state_retention(defer_cleanup),
                child_environment_applied=child_environment_applied,
                result={
                    "changed_path_count": summary.changed_path_count,
                    "final_head": summary.final_head,
                    "lineage_produced": False,
                },
                base_payload=self.workspace_state_payload,
            ),
        )

    def _mark_lineage_worktree_succeeded(
        self,
        child_environment_applied: bool | None,
        defer_cleanup: bool,
    ) -> None:
        worktree_capture = self._require_worktree_capture("Lineage success")
        if self.workspace_kind != "worktree":
            raise RuntimeError("Lineage success requires a worktree workspace.")
        if not self.lineage_producer:
            raise RuntimeError("Lineage success requires a lineage workspace.")
        result = capture_worktree_result(worktree_capture)
        try:
            self._record_lineage_success(
                result,
                child_environment_applied,
                defer_cleanup,
            )
        except Exception as exc:
            cleanup_result_refs_after_failure(
                worktree_capture,
                (result.candidate_ref, result.result_ref),
                exc,
            )
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
        if self.worktree_capture is not None:
            remove_worktree_workspace(self.worktree_capture.source, self.workspace_path)
        else:
            remove_workspace_path(self.workspace_path)
        update_workspace_state(
            self.state_path,
            WorkspaceStateUpdateRequest(
                status="succeeded",
                retention=WorkspaceStateRetention(
                    retention="deleted",
                    retained_reason=None,
                ),
            ),
        )

    def _success_retention(self, defer_cleanup: bool) -> str:
        if self.cleanup_on_success and not defer_cleanup:
            return "deleted"
        if self.cleanup_on_success:
            return "pending_cleanup"
        return "retained"

    def _success_retained_reason(self, defer_cleanup: bool) -> str | None:
        if self.cleanup_on_success:
            return "stage_finalization_pending" if defer_cleanup else None
        return "cleanup_on_success_false"

    def _success_state_retention(self, defer_cleanup: bool) -> WorkspaceStateRetention:
        return WorkspaceStateRetention(
            retention=self._success_retention(defer_cleanup),
            retained_reason=self._success_retained_reason(defer_cleanup),
        )

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
        )

    def _record_lineage_success(
        self,
        result: WorktreeCaptureResult,
        child_environment_applied: bool | None,
        defer_cleanup: bool,
    ) -> None:
        if (
            self.worktree_capture is None
            or self.workspace_path is None
            or self.state_path is None
        ):
            raise RuntimeError("Workspace lineage success requires capture metadata.")
        cache_entry = self._reusable_checkout(result.result_commit, result.result_tree)
        if self.cleanup_on_success and not defer_cleanup:
            remove_worktree_workspace(
                self.worktree_capture.source,
                self.workspace_path,
            )
        elif cache_entry is not None:
            self.reuse_cache.store(cache_entry)
        update_workspace_state(
            self.state_path,
            WorkspaceStateUpdateRequest(
                status="succeeded",
                retention=self._success_state_retention(defer_cleanup),
                child_environment_applied=child_environment_applied,
                result={
                    "candidate_commit": result.candidate_commit,
                    "result_commit": result.result_commit,
                    "candidate_tree": result.candidate_tree,
                    "result_tree": result.result_tree,
                    "changed_path_count": result.changed_path_count,
                    "empty_result": result.changed_path_count == 0,
                    "final_head": result.final_head,
                    "unreachable_provider_objects_scanned": False,
                },
                refs={
                    "candidate": result.candidate_ref,
                    "result": result.result_ref,
                },
                bundle=bundle_descriptor(
                    self.worktree_capture.plan,
                    result,
                    self.worktree_capture.state_path,
                ),
                base_payload=self.workspace_state_payload,
            ),
        )

    def mark_failed(
        self,
        message: str,
        child_environment_applied: bool | None = None,
    ) -> None:
        if self.state_path is None:
            return
        diagnostics = [workspace_diagnostic("error", message)]
        retention, retained_reason = self._terminal_failure_retention(
            "failure",
            diagnostics,
        )
        update_workspace_state(
            self.state_path,
            WorkspaceStateUpdateRequest(
                status="failed",
                diagnostics=diagnostics,
                retention=WorkspaceStateRetention(
                    retention=retention,
                    retained_reason=retained_reason,
                ),
                child_environment_applied=child_environment_applied,
                base_payload=self.workspace_state_payload,
            ),
        )

    def mark_cancelled(
        self,
        message: str,
        child_environment_applied: bool | None = None,
    ) -> None:
        if self.state_path is None:
            return
        diagnostics = [workspace_diagnostic("warning", message)]
        retention, retained_reason = self._terminal_failure_retention(
            "cancelled",
            diagnostics,
        )
        update_workspace_state(
            self.state_path,
            WorkspaceStateUpdateRequest(
                status="cancelled",
                diagnostics=diagnostics,
                retention=WorkspaceStateRetention(
                    retention=retention,
                    retained_reason=retained_reason,
                ),
                child_environment_applied=child_environment_applied,
                base_payload=self.workspace_state_payload,
            ),
        )

    def _terminal_failure_retention(
        self,
        reason: str,
        diagnostics: list[dict[str, str]],
    ) -> tuple[str, str | None]:
        if self.workspace_path is None:
            return "retained", reason
        try:
            if self.workspace_kind == "worktree" and self.worktree_capture is not None:
                self._remove_failed_worktree()
                return "deleted", None
            if self.workspace_kind == "snapshot":
                remove_workspace_path(self.workspace_path)
                return "deleted", None
        except Exception as exc:
            diagnostics.append(
                workspace_diagnostic(
                    "warning",
                    f"Workspace cleanup after terminal invocation state failed: {exc}",
                )
            )
            return "retained", f"{reason}_cleanup_failed"
        if self.workspace_kind == "snapshot":
            return "deleted", None
        return "retained", reason

    def _remove_failed_worktree(self) -> None:
        if self.workspace_path is None or self.worktree_capture is None:
            return
        if self.reuse_cache is not None:
            updated_paths = self.reuse_cache.cleanup_workspace(self.workspace_path)
            if updated_paths:
                return
        remove_worktree_workspace(
            self.worktree_capture.source,
            self.workspace_path,
        )
        if self.reuse_cache is not None:
            self.reuse_cache.discard_workspace(self.workspace_path)
