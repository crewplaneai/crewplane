from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

from crewplane.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSelectionRecord,
)
from crewplane.core.preflight.secrets import SecretContext
from crewplane.runtime.workspace.setup import (
    WorkspaceSetupCancellation,
    WorkspaceSetupError,
    run_workspace_setup,
)
from crewplane.runtime.workspace.state import (
    require_workspace_state_identity,
    update_workspace_setup,
)
from crewplane.runtime.workspace.worktree.reset import worktree_retry_reset
from crewplane.runtime.workspace.worktree.types import WorktreeCaptureRequest

from .common import refresh_trusted_workspace_state_payload


def worktree_retry_reset_with_setup(
    capture_request: WorktreeCaptureRequest,
    plan: PreflightExecutionPlan,
    policy: WorkspaceSelectionRecord,
    cwd: Path,
    state_path: Path,
    checkout_root: Path,
    trusted_state_payload: dict[str, object],
    secret_context: SecretContext,
) -> Callable[[], None]:
    return _WorktreeRetryReset(
        capture_request=capture_request,
        plan=plan,
        policy=policy,
        cwd=cwd,
        state_path=state_path,
        checkout_root=checkout_root,
        trusted_state_payload=trusted_state_payload,
        secret_context=secret_context,
    )


@dataclass
class _WorktreeRetryReset:
    capture_request: WorktreeCaptureRequest
    plan: PreflightExecutionPlan
    policy: WorkspaceSelectionRecord
    cwd: Path
    state_path: Path
    checkout_root: Path
    trusted_state_payload: dict[str, object]
    secret_context: SecretContext
    _lock: Lock = field(default_factory=Lock)
    _cancelled: bool = False
    _setup_cancellation: WorkspaceSetupCancellation | None = None

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            setup_cancellation = self._setup_cancellation
        if setup_cancellation is not None:
            setup_cancellation.cancel()

    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def __call__(self) -> None:
        try:
            self._reset_and_setup()
        finally:
            with self._lock:
                self._cancelled = False

    def _reset_and_setup(self) -> None:
        require_workspace_state_identity(
            self.state_path,
            self.trusted_state_payload,
        )
        worktree_retry_reset(self.capture_request, self.is_cancelled)()
        if self.is_cancelled():
            raise RuntimeError("Workspace retry reset was cancelled.")
        setup_cancellation = self._new_setup_cancellation()
        try:
            setup_summary = run_workspace_setup(
                self.plan,
                self.policy,
                self.cwd,
                self.state_path,
                self.checkout_root,
                setup_cancellation,
                self.secret_context,
            )
        except WorkspaceSetupError as exc:
            self._record_setup(exc.summary)
            raise
        finally:
            self._clear_setup_cancellation(setup_cancellation)
        if setup_summary is not None:
            self._record_setup(setup_summary)

    def _record_setup(self, setup_summary: Mapping[str, object]) -> None:
        update_workspace_setup(
            self.state_path,
            setup_summary,
            base_payload=self.trusted_state_payload,
        )
        refresh_trusted_workspace_state_payload(
            self.trusted_state_payload,
            self.state_path,
        )

    def _new_setup_cancellation(self) -> WorkspaceSetupCancellation:
        setup_cancellation = WorkspaceSetupCancellation()
        with self._lock:
            self._setup_cancellation = setup_cancellation
            cancelled = self._cancelled
        if cancelled:
            setup_cancellation.cancel()
        return setup_cancellation

    def _clear_setup_cancellation(
        self,
        setup_cancellation: WorkspaceSetupCancellation,
    ) -> None:
        with self._lock:
            if self._setup_cancellation is setup_cancellation:
                self._setup_cancellation = None


def worktree_retry_reset_canceller(
    retry_reset: Callable[[], None],
) -> Callable[[], None] | None:
    canceller = getattr(retry_reset, "cancel", None)
    return canceller if callable(canceller) else None
