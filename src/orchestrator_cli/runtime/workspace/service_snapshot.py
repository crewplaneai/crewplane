from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

from orchestrator_cli.architecture.contracts import (
    InvocationContext,
    InvocationSourceContext,
    InvocationWorkspaceContext,
    InvocationWorktreeContract,
)
from orchestrator_cli.core.preflight.models import (
    PreflightExecutionNode,
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
from orchestrator_cli.runtime.workspace.snapshot import (
    create_snapshot_workspace,
    materialize_snapshot,
    runtime_workspace_cache_root,
    snapshot_entries,
    snapshot_retry_reset,
)
from orchestrator_cli.runtime.workspace.state import (
    WorkspaceProvisioningMetadata,
    WorkspaceStateMaterializationRequest,
    write_running_workspace_state,
)
from orchestrator_cli.runtime.workspace.worktree_cleanup import worktree_disk_usage

from .service_common import (
    planned_workspace_path,
    record_failed_preparation_state,
    remove_workspace_after_failure,
    trusted_workspace_state_payload,
    workspace_cwd,
    workspace_state_request,
)
from .service_types import (
    MaterializedSnapshotWorkspace,
    SnapshotPreparationPlan,
    WorkspaceInvocationRequest,
)


def prepare_snapshot_invocation_workspace(
    request: WorkspaceInvocationRequest,
    invocation_context: InvocationContext,
    node: PreflightExecutionNode,
    policy: WorkspaceSelectionRecord,
    source: WorkspaceSourceSnapshot,
) -> PreparedWorkspace:
    snapshot_plan = snapshot_preparation_plan(request, node, policy, source)
    write_running_snapshot_workspace_state(request, snapshot_plan)
    materialized_snapshot = materialize_snapshot_workspace(request, snapshot_plan)
    try:
        return prepared_snapshot_workspace(
            request,
            invocation_context,
            snapshot_plan,
            materialized_snapshot,
        )
    except Exception as exc:
        removed = remove_workspace_after_failure(
            materialized_snapshot.workspace_path,
            exc,
        )
        record_failed_preparation_state(
            snapshot_plan.state_path,
            exc,
            workspace_retention="deleted" if removed else "retained",
            retained_reason=None if removed else "preparation_failed_cleanup_failed",
        )
        raise


def snapshot_preparation_plan(
    request: WorkspaceInvocationRequest,
    node: PreflightExecutionNode,
    policy: WorkspaceSelectionRecord,
    source: WorkspaceSourceSnapshot,
) -> SnapshotPreparationPlan:
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
    workspace_path = planned_workspace_path(request.plan, source, "snapshots", slug)
    return SnapshotPreparationPlan(
        node=node,
        policy=policy,
        source=source,
        slug=slug,
        state_path=state_path,
        child_environment_required=controlled_child_environment_required(request.plan),
        planned_workspace_path=workspace_path,
    )


def write_running_snapshot_workspace_state(
    request: WorkspaceInvocationRequest,
    plan: SnapshotPreparationPlan,
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
            materialization=plan.policy.materialization,
        ),
    )


def materialize_snapshot_workspace(
    request: WorkspaceInvocationRequest,
    plan: SnapshotPreparationPlan,
) -> MaterializedSnapshotWorkspace:
    with workspace_materialization_slot(request.plan, request.materialization_limiter):
        provisioning_started = monotonic()
        try:
            workspace_path = create_snapshot_workspace(
                request.plan,
                plan.slug,
                plan.source,
            )
        except Exception as exc:
            record_failed_preparation_state(plan.state_path, exc)
            raise
        try:
            checkout_root = workspace_path / "checkout"
            with TemporaryDirectory(prefix="orchestrator-cli-index-") as index_dir:
                materialize_snapshot(
                    plan.source,
                    checkout_root,
                    Path(index_dir) / "snapshot.index",
                )
            initial_snapshot_entries = snapshot_entries(checkout_root)
        except Exception as exc:
            removed = remove_workspace_after_failure(workspace_path, exc)
            record_failed_preparation_state(
                plan.state_path,
                exc,
                workspace_retention="deleted" if removed else "retained",
                retained_reason=None
                if removed
                else "preparation_failed_cleanup_failed",
            )
            raise
        provisioning_duration_seconds = round(monotonic() - provisioning_started, 6)
    cwd = workspace_cwd(checkout_root, plan.source)
    return MaterializedSnapshotWorkspace(
        workspace_path=workspace_path,
        checkout_root=checkout_root,
        cwd=cwd,
        initial_snapshot_entries=initial_snapshot_entries,
        provisioning_duration_seconds=provisioning_duration_seconds,
        checkout_size_bytes=worktree_disk_usage(checkout_root),
    )


def prepared_snapshot_workspace(
    request: WorkspaceInvocationRequest,
    invocation_context: InvocationContext,
    plan: SnapshotPreparationPlan,
    materialized: MaterializedSnapshotWorkspace,
) -> PreparedWorkspace:
    workspace_context = InvocationWorkspaceContext(
        workspace_kind="snapshot",
        materialization="snapshot_checkout",
        logical_worktree_name=plan.policy.logical_worktree_name or "",
        cwd=materialized.cwd,
        invocation_source=InvocationSourceContext(
            source_kind="project",
            source_node_id=None,
            source_commit=plan.source.run_base_commit,
            source_tree=plan.source.source_tree,
        ),
        worktree_contract=InvocationWorktreeContract(
            mode=plan.policy.worktree_contract.mode,
            schema_version=plan.policy.worktree_contract.schema_version,
        ),
        checkout_root=materialized.checkout_root,
        writable=True,
        lineage_producer=False,
        workspace_state_path=plan.state_path,
        child_environment_required=plan.child_environment_required,
        child_environment_applied=False if plan.child_environment_required else None,
    )
    effective_context = replace(
        invocation_context,
        workspace=workspace_context,
        retry_reset=snapshot_retry_reset(plan.source, materialized.checkout_root),
    )
    write_running_workspace_state(
        plan.state_path,
        workspace_state_request(request),
        plan.node,
        plan.source,
        plan.policy,
        WorkspaceStateMaterializationRequest(
            workspace_path=materialized.workspace_path,
            child_environment_required=plan.child_environment_required,
            cache_root=runtime_workspace_cache_root(request.plan),
            effective_cwd=materialized.cwd,
            checkout_root=materialized.checkout_root,
            provisioning=WorkspaceProvisioningMetadata(
                checkout_size_bytes=materialized.checkout_size_bytes,
                duration_seconds=materialized.provisioning_duration_seconds,
            ),
            materialization=plan.policy.materialization,
        ),
    )
    return PreparedWorkspace(
        cwd=materialized.cwd,
        invocation_context=effective_context,
        workspace_kind="snapshot",
        workspace_path=materialized.workspace_path,
        state_path=plan.state_path,
        initial_snapshot_entries=materialized.initial_snapshot_entries,
        cleanup_on_success=workspace_cleanup_on_success(request.plan),
        workspace_state_payload=trusted_workspace_state_payload(plan.state_path),
    )
