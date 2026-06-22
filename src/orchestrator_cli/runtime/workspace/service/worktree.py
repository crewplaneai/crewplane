from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Lock
from time import monotonic

from orchestrator_cli.architecture.contracts import (
    InvocationContext,
    InvocationSourceContext,
    InvocationWorkspaceContext,
    InvocationWorktreeContract,
)
from orchestrator_cli.core.preflight.models import (
    PreflightExecutionNode,
    PreflightExecutionPlan,
    WorkspaceSelectionRecord,
    WorkspaceSourceSnapshot,
)
from orchestrator_cli.runtime.workspace.invocation import (
    controlled_child_environment_required,
    invocation_slug,
    workspace_cleanup_on_success,
    workspace_state_path,
)
from orchestrator_cli.runtime.workspace.materialization import (
    workspace_materialization_slot,
)
from orchestrator_cli.runtime.workspace.prepared_workspace import PreparedWorkspace
from orchestrator_cli.runtime.workspace.setup import (
    WorkspaceSetupCancellation,
    WorkspaceSetupCancelled,
    WorkspaceSetupError,
    run_workspace_setup,
)
from orchestrator_cli.runtime.workspace.snapshot import (
    runtime_workspace_cache_root,
    snapshot_entries,
)
from orchestrator_cli.runtime.workspace.state import (
    WorkspaceProvisioningMetadata,
    WorkspaceStateMaterializationRequest,
    update_workspace_setup,
    write_running_workspace_state,
)
from orchestrator_cli.runtime.workspace.worktree import WorktreeCaptureRequest
from orchestrator_cli.runtime.workspace.worktree.cleanup import worktree_disk_usage
from orchestrator_cli.runtime.workspace.worktree.lineage import (
    worktree_protected_ref_scopes,
)
from orchestrator_cli.runtime.workspace.worktree.materialization import (
    materialize_worktree_workspace,
)
from orchestrator_cli.runtime.workspace.worktree.reset import worktree_retry_reset
from orchestrator_cli.runtime.workspace.worktree.source_refs import (
    invocation_source_ref,
)

from .common import (
    planned_workspace_path,
    refresh_trusted_workspace_state_payload,
    workspace_state_request,
)
from .types import (
    MaterializedWorktreeWorkspace,
    WorkspaceInvocationRequest,
    WorktreePreparationPlan,
)
from .worktree_failures import (
    record_cancelled_worktree_preparation,
    record_failed_unmaterialized_worktree_preparation,
    record_failed_worktree_preparation,
)


def prepare_worktree_invocation_workspace(
    request: WorkspaceInvocationRequest,
    invocation_context: InvocationContext,
    node: PreflightExecutionNode,
    policy: WorkspaceSelectionRecord,
    source: WorkspaceSourceSnapshot,
) -> PreparedWorkspace:
    worktree_plan = worktree_preparation_plan(request, node, policy, source)
    write_running_worktree_workspace_state(request, worktree_plan)
    materialized_worktree = materialize_worktree_invocation_workspace(
        request,
        worktree_plan,
    )
    try:
        return prepared_worktree_workspace(
            request,
            invocation_context,
            worktree_plan,
            materialized_worktree,
        )
    except WorkspaceSetupCancelled as exc:
        record_cancelled_worktree_preparation(
            source,
            materialized_worktree.materialized.worktree.workspace_path,
            worktree_plan.state_path,
            exc,
        )
        raise
    except Exception as exc:
        record_failed_worktree_preparation(
            source,
            materialized_worktree.materialized.worktree.workspace_path,
            worktree_plan.state_path,
            exc,
        )
        raise


def worktree_preparation_plan(
    request: WorkspaceInvocationRequest,
    node: PreflightExecutionNode,
    policy: WorkspaceSelectionRecord,
    source: WorkspaceSourceSnapshot,
) -> WorktreePreparationPlan:
    source_ref = invocation_source_ref(
        request.output,
        node,
        policy,
        source,
        request.role_label,
        request.round_num,
        request.audit_round_num,
    )
    slug = invocation_slug(
        request.node_id,
        request.task_id,
        request.audit_round_num,
        request.round_num,
    )
    state_path = workspace_state_path(
        request.output,
        node,
        slug,
        request.audit_round_num,
        request.round_num,
    )
    lineage_producer = request.role_label == "executor"
    protected_ref_scopes = worktree_protected_ref_scopes(
        request.plan,
        source_ref,
        node.id,
        slug,
    )
    workspace_path = planned_workspace_path(
        request.plan,
        source,
        "workspaces" if lineage_producer else "review-workspaces",
        slug,
        None if lineage_producer else node.id,
    )
    return WorktreePreparationPlan(
        node=node,
        policy=policy,
        source=source,
        source_ref=source_ref,
        slug=slug,
        state_path=state_path,
        child_environment_required=controlled_child_environment_required(request.plan),
        lineage_producer=lineage_producer,
        protected_ref_scopes=protected_ref_scopes,
        planned_workspace_path=workspace_path,
    )


def write_running_worktree_workspace_state(
    request: WorkspaceInvocationRequest,
    plan: WorktreePreparationPlan,
) -> None:
    write_running_workspace_state(
        plan.state_path,
        workspace_state_request(request),
        plan.node,
        plan.source,
        plan.policy,
        WorkspaceStateMaterializationRequest(
            workspace_path=plan.planned_workspace_path,
            child_environment_required=plan.child_environment_required,
            cache_root=runtime_workspace_cache_root(request.plan),
            checkout_root=plan.planned_workspace_path / "checkout",
            source_ref=plan.source_ref,
            materialization=plan.policy.materialization,
            writable=True,
            lineage_producer=plan.lineage_producer,
        ),
    )


def materialize_worktree_invocation_workspace(
    request: WorkspaceInvocationRequest,
    plan: WorktreePreparationPlan,
) -> MaterializedWorktreeWorkspace:
    with workspace_materialization_slot(request.plan, request.materialization_limiter):
        provisioning_started = monotonic()
        try:
            materialized = materialize_worktree_workspace(
                request.plan,
                plan.slug,
                plan.source,
                plan.source_ref,
                plan.protected_ref_scopes,
                parent_slug=None if plan.lineage_producer else plan.node.id,
                logical_worktree_name=plan.policy.logical_worktree_name,
                lineage_producer=plan.lineage_producer,
                reuse_cache=request.worktree_reuse_cache,
            )
        except Exception as exc:
            record_failed_unmaterialized_worktree_preparation(plan, exc)
            raise
        provisioning_duration_seconds = round(monotonic() - provisioning_started, 6)
    worktree = materialized.worktree
    capture_request = WorktreeCaptureRequest(
        plan=request.plan,
        source=plan.source,
        source_ref=plan.source_ref,
        workspace_path=worktree.workspace_path,
        checkout_root=worktree.checkout_root,
        git_dir=worktree.git_dir,
        node_id=plan.node.id,
        task_id=request.task_id,
        state_path=plan.state_path,
        slug=plan.slug,
        protected_refs=worktree.protected_refs,
    )
    return MaterializedWorktreeWorkspace(
        materialized=materialized,
        capture_request=capture_request,
        provisioning_duration_seconds=provisioning_duration_seconds,
        checkout_size_bytes=worktree_disk_usage(worktree.checkout_root),
    )


def prepared_worktree_workspace(
    request: WorkspaceInvocationRequest,
    invocation_context: InvocationContext,
    plan: WorktreePreparationPlan,
    materialized_workspace: MaterializedWorktreeWorkspace,
) -> PreparedWorkspace:
    worktree = materialized_workspace.materialized.worktree
    workspace_context = InvocationWorkspaceContext(
        workspace_kind="worktree",
        materialization="worktree_checkout",
        logical_worktree_name=plan.policy.logical_worktree_name or "",
        cwd=worktree.cwd,
        invocation_source=InvocationSourceContext(
            source_kind=plan.source_ref.source_kind,
            source_node_id=plan.source_ref.source_node_id,
            source_commit=plan.source_ref.source_commit,
            source_tree=plan.source_ref.source_tree,
            candidate_sequence=plan.source_ref.candidate_sequence,
        ),
        worktree_contract=InvocationWorktreeContract(
            mode=plan.policy.worktree_contract.mode,
            schema_version=plan.policy.worktree_contract.schema_version,
        ),
        checkout_root=worktree.checkout_root,
        writable=True,
        lineage_producer=plan.lineage_producer,
        workspace_state_path=plan.state_path,
        child_environment_required=plan.child_environment_required,
        child_environment_applied=False if plan.child_environment_required else None,
    )
    trusted_state_payload: dict[str, object] = {}
    retry_reset = worktree_retry_reset_with_setup(
        materialized_workspace.capture_request,
        request.plan,
        plan.policy,
        worktree.cwd,
        plan.state_path,
        worktree.checkout_root,
        trusted_state_payload,
    )
    effective_context = replace(
        invocation_context,
        workspace=workspace_context,
        retry_reset=retry_reset,
        retry_reset_canceller=worktree_retry_reset_canceller(retry_reset),
    )
    write_materialized_worktree_state(request, plan, materialized_workspace)
    refresh_trusted_workspace_state_payload(trusted_state_payload, plan.state_path)
    run_worktree_setup(
        request,
        plan,
        worktree.cwd,
        worktree.checkout_root,
        trusted_state_payload,
    )
    initial_snapshot_entries = snapshot_entries(worktree.checkout_root)
    return PreparedWorkspace(
        cwd=worktree.cwd,
        invocation_context=effective_context,
        workspace_kind="worktree",
        workspace_path=worktree.workspace_path,
        state_path=plan.state_path,
        initial_snapshot_entries=initial_snapshot_entries,
        cleanup_on_success=workspace_cleanup_on_success(request.plan),
        lineage_producer=plan.lineage_producer,
        worktree_capture=materialized_workspace.capture_request,
        reuse_cache=request.worktree_reuse_cache,
        reuse_key=plan.policy.logical_worktree_name,
        workspace_state_payload=trusted_state_payload,
    )


def write_materialized_worktree_state(
    request: WorkspaceInvocationRequest,
    plan: WorktreePreparationPlan,
    materialized_workspace: MaterializedWorktreeWorkspace,
) -> None:
    worktree = materialized_workspace.materialized.worktree
    write_running_workspace_state(
        plan.state_path,
        workspace_state_request(request),
        plan.node,
        plan.source,
        plan.policy,
        WorkspaceStateMaterializationRequest(
            workspace_path=worktree.workspace_path,
            child_environment_required=plan.child_environment_required,
            cache_root=runtime_workspace_cache_root(request.plan),
            effective_cwd=worktree.cwd,
            checkout_root=worktree.checkout_root,
            provisioning=WorkspaceProvisioningMetadata(
                checkout_size_bytes=materialized_workspace.checkout_size_bytes,
                duration_seconds=materialized_workspace.provisioning_duration_seconds,
            ),
            source_ref=plan.source_ref,
            materialization=plan.policy.materialization,
            writable=True,
            lineage_producer=plan.lineage_producer,
            worktree_lock_mode=worktree.lock_mode,
            reuse=materialized_workspace.materialized.reuse,
        ),
    )


def run_worktree_setup(
    request: WorkspaceInvocationRequest,
    plan: WorktreePreparationPlan,
    cwd: Path,
    checkout_root: Path,
    trusted_state_payload: dict[str, object],
) -> None:
    setup_summary = run_workspace_setup(
        request.plan,
        plan.policy,
        cwd,
        plan.state_path,
        checkout_root,
        request.setup_cancellation,
    )
    if setup_summary is None:
        return
    update_workspace_setup(
        plan.state_path,
        setup_summary,
        base_payload=trusted_state_payload,
    )
    refresh_trusted_workspace_state_payload(trusted_state_payload, plan.state_path)


def worktree_retry_reset_with_setup(
    capture_request: WorktreeCaptureRequest,
    plan: PreflightExecutionPlan,
    policy: WorkspaceSelectionRecord,
    cwd: Path,
    state_path: Path,
    checkout_root: Path,
    trusted_state_payload: dict[str, object],
) -> Callable[[], None]:
    reset = worktree_retry_reset(capture_request)
    if policy.setup is None:
        return reset
    return _WorktreeRetryResetWithSetup(
        reset_workspace=reset,
        plan=plan,
        policy=policy,
        cwd=cwd,
        state_path=state_path,
        checkout_root=checkout_root,
        trusted_state_payload=trusted_state_payload,
    )


@dataclass
class _WorktreeRetryResetWithSetup:
    reset_workspace: Callable[[], None]
    plan: PreflightExecutionPlan
    policy: WorkspaceSelectionRecord
    cwd: Path
    state_path: Path
    checkout_root: Path
    trusted_state_payload: dict[str, object]
    _lock: Lock = field(default_factory=Lock)
    _cancelled: bool = False
    _setup_cancellation: WorkspaceSetupCancellation | None = None

    def cancel(self) -> None:
        setup_cancellation: WorkspaceSetupCancellation | None = None
        with self._lock:
            self._cancelled = True
            setup_cancellation = self._setup_cancellation
        if setup_cancellation is not None:
            setup_cancellation.cancel()

    def __call__(self) -> None:
        try:
            self._reset_and_setup()
        finally:
            with self._lock:
                self._cancelled = False

    def _reset_and_setup(self) -> None:
        self.reset_workspace()
        setup_cancellation = self._new_setup_cancellation()
        try:
            setup_summary = run_workspace_setup(
                self.plan,
                self.policy,
                self.cwd,
                self.state_path,
                self.checkout_root,
                setup_cancellation,
            )
        except WorkspaceSetupError as exc:
            update_workspace_setup(
                self.state_path,
                exc.summary,
                base_payload=self.trusted_state_payload,
            )
            refresh_trusted_workspace_state_payload(
                self.trusted_state_payload,
                self.state_path,
            )
            raise
        finally:
            self._clear_setup_cancellation(setup_cancellation)
        if setup_summary is None:
            return
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
