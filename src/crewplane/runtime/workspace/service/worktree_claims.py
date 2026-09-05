from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from crewplane.runtime.workspace.snapshot import runtime_workspace_cache_root
from crewplane.runtime.workspace.state import (
    WorkspaceStateMaterializationRequest,
    write_running_workspace_state,
)
from crewplane.runtime.workspace.worktree.cache import ReusableWorktreeCheckout
from crewplane.runtime.workspace.worktree.materialization import (
    WorktreeClaimCallbacks,
)
from crewplane.runtime.workspace.worktree.types import WorktreeProvisioningClaim

from .common import workspace_state_request
from .types import WorkspaceInvocationRequest, WorktreePreparationPlan


@dataclass(frozen=True, slots=True)
class _WorktreeClaim:
    workspace_path: Path
    cwd: Path
    checkout_root: Path
    git_dir: Path
    lock_mode: str
    reuse: Mapping[str, object]
    generation: int


@dataclass(slots=True)
class WorktreeClaimWriter:
    request: WorkspaceInvocationRequest
    plan: WorktreePreparationPlan
    fresh_recorded: bool = False

    def callbacks(self) -> WorktreeClaimCallbacks:
        return WorktreeClaimCallbacks(
            record_reuse=self.record_reuse,
            record_fresh=self.record_fresh,
        )

    def record_reuse(
        self,
        reusable: ReusableWorktreeCheckout,
        generation: int,
    ) -> None:
        self._write(
            _WorktreeClaim(
                workspace_path=reusable.workspace_path,
                cwd=reusable.cwd,
                checkout_root=reusable.checkout_root,
                git_dir=reusable.git_dir,
                lock_mode="reuse_leased",
                reuse={
                    "strategy": "incremental_reset",
                    "reused": True,
                    "fallback": False,
                    "previous_workspace_state": reusable.state_path.name,
                },
                generation=generation,
            )
        )

    def record_fresh(
        self,
        worktree: WorktreeProvisioningClaim,
        generation: int,
    ) -> None:
        self._write(
            _WorktreeClaim(
                workspace_path=worktree.workspace_path,
                cwd=worktree.cwd,
                checkout_root=worktree.checkout_root,
                git_dir=worktree.git_dir,
                lock_mode=worktree.lock_mode,
                reuse={
                    "strategy": "fresh_checkout",
                    "reused": False,
                    "fallback": False,
                },
                generation=generation,
            )
        )
        self.fresh_recorded = True

    def _write(self, claim: _WorktreeClaim) -> None:
        request = self.request
        plan = self.plan
        write_running_workspace_state(
            plan.state_path,
            workspace_state_request(request),
            plan.node,
            plan.source,
            plan.policy,
            WorkspaceStateMaterializationRequest(
                workspace_path=claim.workspace_path,
                child_environment_required=plan.child_environment_required,
                cache_root=runtime_workspace_cache_root(request.plan),
                effective_cwd=claim.cwd,
                checkout_root=claim.checkout_root,
                worktree_git_dir=claim.git_dir,
                source_ref=plan.source_ref,
                materialization=plan.policy.materialization,
                writable=True,
                lineage_producer=plan.lineage_producer,
                worktree_lock_mode=claim.lock_mode,
                reuse=claim.reuse,
                reuse_generation=claim.generation,
            ),
        )
