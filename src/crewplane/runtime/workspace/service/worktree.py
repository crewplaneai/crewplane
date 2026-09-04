from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from time import monotonic

from crewplane.architecture.contracts import (
    InvocationContext,
    InvocationSourceContext,
    InvocationWorkspaceContext,
    InvocationWorktreeContract,
)
from crewplane.core.preflight.models import (
    PreflightExecutionNode,
    WorkspaceSelectionRecord,
    WorkspaceSourceSnapshot,
)
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.workspace.invocation import (
    controlled_child_environment_required,
    invocation_slug,
    workspace_cleanup_on_success,
    workspace_state_path,
)
from crewplane.runtime.workspace.prepared_workspace import PreparedWorkspace
from crewplane.runtime.workspace.setup import (
    WorkspaceSetupCancelled,
    run_workspace_setup,
)
from crewplane.runtime.workspace.snapshot import (
    WorkspaceSnapshotPolicy,
    runtime_workspace_cache_root,
    snapshot_entries,
)
from crewplane.runtime.workspace.state import (
    WorkspaceProvisioningMetadata,
    WorkspaceStateMaterializationRequest,
    read_workspace_state,
    write_running_workspace_state,
)
from crewplane.runtime.workspace.state_evidence import update_workspace_setup
from crewplane.runtime.workspace.worktree import WorktreeCaptureRequest
from crewplane.runtime.workspace.worktree.cache import ReusableWorktreeCheckout
from crewplane.runtime.workspace.worktree.cleanup import worktree_disk_usage
from crewplane.runtime.workspace.worktree.lineage import (
    worktree_protected_ref_scopes,
)
from crewplane.runtime.workspace.worktree.materialization import (
    materialize_worktree_workspace,
)
from crewplane.runtime.workspace.worktree.source_refs import (
    invocation_source_ref,
)
from crewplane.runtime.workspace.worktree.types import WorktreeProvisioningClaim

from .common import (
    planned_workspace_path,
    refresh_trusted_workspace_state_payload,
    workspace_state_request,
)
from .retry_reset import (
    worktree_retry_reset_canceller,
    worktree_retry_reset_with_setup,
)
from .types import (
    MaterializedWorktreeWorkspace,
    WorkspaceInvocationRequest,
    WorktreePreparationPlan,
)
from .worktree_failures import (
    record_cancelled_unmaterialized_worktree_preparation,
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
            request.worktree_reuse_cache,
        )
        raise
    except Exception as exc:
        record_failed_worktree_preparation(
            source,
            materialized_worktree.materialized.worktree.workspace_path,
            worktree_plan.state_path,
            exc,
            request.worktree_reuse_cache,
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
        request.plan,
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
    lineage_producer = request.role_label == ProviderRole.EXECUTOR
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
    cancel_requested = (
        request.setup_cancellation.is_cancelled
        if request.setup_cancellation is not None
        else None
    )
    fresh_claim_recorded = False

    def record_reuse_claim(
        reusable: ReusableWorktreeCheckout,
        reuse_generation: int,
    ) -> None:
        write_running_workspace_state(
            plan.state_path,
            workspace_state_request(request),
            plan.node,
            plan.source,
            plan.policy,
            WorkspaceStateMaterializationRequest(
                workspace_path=reusable.workspace_path,
                child_environment_required=plan.child_environment_required,
                cache_root=runtime_workspace_cache_root(request.plan),
                effective_cwd=reusable.cwd,
                checkout_root=reusable.checkout_root,
                worktree_git_dir=reusable.git_dir,
                source_ref=plan.source_ref,
                materialization=plan.policy.materialization,
                writable=True,
                lineage_producer=plan.lineage_producer,
                worktree_lock_mode="reuse_leased",
                reuse={
                    "strategy": "incremental_reset",
                    "reused": True,
                    "fallback": False,
                    "previous_workspace_state": reusable.state_path.name,
                },
                reuse_generation=reuse_generation,
            ),
        )

    def record_fresh_claim(
        worktree: WorktreeProvisioningClaim,
        reuse_generation: int,
    ) -> None:
        nonlocal fresh_claim_recorded
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
                worktree_git_dir=worktree.git_dir,
                source_ref=plan.source_ref,
                materialization=plan.policy.materialization,
                writable=True,
                lineage_producer=plan.lineage_producer,
                worktree_lock_mode=worktree.lock_mode,
                reuse={
                    "strategy": "fresh_checkout",
                    "reused": False,
                    "fallback": False,
                },
                reuse_generation=reuse_generation,
            ),
        )
        fresh_claim_recorded = True

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
            record_reuse_claim=record_reuse_claim,
            record_fresh_claim=record_fresh_claim,
            materialization_limiter=request.materialization_limiter,
            planned_workspace_path=plan.planned_workspace_path,
            state_path=plan.state_path,
            cancel_requested=cancel_requested,
        )
    except Exception as exc:
        if cancel_requested is not None and cancel_requested():
            if read_workspace_state(plan.state_path).get("status") != "cancelled":
                if fresh_claim_recorded:
                    record_cancelled_worktree_preparation(
                        plan.source,
                        plan.planned_workspace_path,
                        plan.state_path,
                        exc,
                        request.worktree_reuse_cache,
                    )
                else:
                    record_cancelled_unmaterialized_worktree_preparation(plan, exc)
            raise
        if fresh_claim_recorded:
            record_failed_worktree_preparation(
                plan.source,
                plan.planned_workspace_path,
                plan.state_path,
                exc,
                request.worktree_reuse_cache,
            )
        else:
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
        request.secret_context,
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
    initial_snapshot_entries = snapshot_entries(
        worktree.checkout_root,
        WorkspaceSnapshotPolicy(
            cancel_requested=(
                request.setup_cancellation.is_cancelled
                if request.setup_cancellation is not None
                else None
            ),
        ),
    )
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
        snapshot_cancel_requested=(
            request.setup_cancellation.is_cancelled
            if request.setup_cancellation is not None
            else None
        ),
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
            worktree_git_dir=worktree.git_dir,
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
            reuse_generation=materialized_workspace.materialized.reuse_generation,
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
        request.secret_context,
    )
    if setup_summary is None:
        return
    update_workspace_setup(
        plan.state_path,
        setup_summary,
        base_payload=trusted_state_payload,
    )
    refresh_trusted_workspace_state_payload(trusted_state_payload, plan.state_path)
