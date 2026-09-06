from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
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
from crewplane.runtime.workspace.invocation import (
    controlled_child_environment_required,
    invocation_slug,
    workspace_cleanup_on_success,
    workspace_state_path,
)
from crewplane.runtime.workspace.materialization import (
    MaterializationCapacityRequest,
    workspace_materialization_slot,
)
from crewplane.runtime.workspace.prepared_workspace import PreparedWorkspace
from crewplane.runtime.workspace.snapshot import (
    WorkspaceSnapshotPolicy,
    create_snapshot_workspace,
    materialize_snapshot,
    runtime_workspace_cache_root,
    snapshot_entries,
    snapshot_retry_reset,
)
from crewplane.runtime.workspace.state import (
    WorkspaceProvisioningMetadata,
    WorkspaceStateMaterializationRequest,
    write_running_workspace_state,
)
from crewplane.runtime.workspace.worktree.cleanup import worktree_disk_usage

from .common import (
    planned_workspace_path,
    trusted_workspace_state_payload,
    workspace_cwd,
    workspace_state_request,
)
from .snapshot_failures import (
    record_failed_materialized_snapshot_preparation,
    record_failed_unmaterialized_snapshot_preparation,
    terminalize_unhandled_snapshot_materialization_failure,
)
from .types import (
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
    materialized_snapshot = _materialize_snapshot_with_failure_state(
        request,
        snapshot_plan,
    )
    return _prepare_materialized_snapshot_with_failure_state(
        request,
        invocation_context,
        snapshot_plan,
        materialized_snapshot,
    )


def _prepare_materialized_snapshot_with_failure_state(
    request: WorkspaceInvocationRequest,
    invocation_context: InvocationContext,
    plan: SnapshotPreparationPlan,
    materialized: MaterializedSnapshotWorkspace,
) -> PreparedWorkspace:
    try:
        return prepared_snapshot_workspace(
            request,
            invocation_context,
            plan,
            materialized,
        )
    except Exception as exc:
        record_failed_materialized_snapshot_preparation(
            plan,
            materialized.workspace_path,
            exc,
        )
        raise


def _materialize_snapshot_with_failure_state(
    request: WorkspaceInvocationRequest,
    plan: SnapshotPreparationPlan,
) -> MaterializedSnapshotWorkspace:
    try:
        return materialize_snapshot_workspace(request, plan)
    except Exception as exc:
        terminalize_unhandled_snapshot_materialization_failure(plan, exc)
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
    with workspace_materialization_slot(
        request.plan,
        request.materialization_limiter,
        MaterializationCapacityRequest(plan.planned_workspace_path, plan.source),
    ):
        provisioning_started = monotonic()
        workspace_path = _create_snapshot_workspace_with_failure_state(request, plan)
        checkout_root, initial_snapshot_entries = (
            _materialize_snapshot_checkout_with_failure_state(
                request,
                plan,
                workspace_path,
            )
        )
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


def _create_snapshot_workspace_with_failure_state(
    request: WorkspaceInvocationRequest,
    plan: SnapshotPreparationPlan,
) -> Path:
    try:
        return create_snapshot_workspace(request.plan, plan.slug, plan.source)
    except Exception as exc:
        record_failed_unmaterialized_snapshot_preparation(plan, exc)
        raise


def _materialize_snapshot_checkout_with_failure_state(
    request: WorkspaceInvocationRequest,
    plan: SnapshotPreparationPlan,
    workspace_path: Path,
) -> tuple[Path, dict[str, str]]:
    try:
        return _populate_snapshot_checkout(request, plan, workspace_path)
    except Exception as exc:
        record_failed_materialized_snapshot_preparation(plan, workspace_path, exc)
        raise


def _populate_snapshot_checkout(
    request: WorkspaceInvocationRequest,
    plan: SnapshotPreparationPlan,
    workspace_path: Path,
) -> tuple[Path, dict[str, str]]:
    checkout_root = workspace_path / "checkout"
    with TemporaryDirectory(prefix="crewplane-index-") as index_dir:
        materialize_snapshot(
            plan.source,
            checkout_root,
            Path(index_dir) / "snapshot.index",
        )
    entries = snapshot_entries(
        checkout_root,
        WorkspaceSnapshotPolicy(cancel_requested=_snapshot_cancel_requested(request)),
    )
    return checkout_root, entries


def prepared_snapshot_workspace(
    request: WorkspaceInvocationRequest,
    invocation_context: InvocationContext,
    plan: SnapshotPreparationPlan,
    materialized: MaterializedSnapshotWorkspace,
) -> PreparedWorkspace:
    effective_context = replace(
        invocation_context,
        workspace=_build_snapshot_workspace_context(plan, materialized),
        retry_reset=snapshot_retry_reset(plan.source, materialized.checkout_root),
    )
    write_materialized_snapshot_workspace_state(request, plan, materialized)
    return _build_prepared_snapshot_workspace(
        request,
        effective_context,
        plan,
        materialized,
    )


def _build_snapshot_workspace_context(
    plan: SnapshotPreparationPlan,
    materialized: MaterializedSnapshotWorkspace,
) -> InvocationWorkspaceContext:
    return InvocationWorkspaceContext(
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


def write_materialized_snapshot_workspace_state(
    request: WorkspaceInvocationRequest,
    plan: SnapshotPreparationPlan,
    materialized: MaterializedSnapshotWorkspace,
) -> None:
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


def _build_prepared_snapshot_workspace(
    request: WorkspaceInvocationRequest,
    invocation_context: InvocationContext,
    plan: SnapshotPreparationPlan,
    materialized: MaterializedSnapshotWorkspace,
) -> PreparedWorkspace:
    return PreparedWorkspace(
        cwd=materialized.cwd,
        invocation_context=invocation_context,
        workspace_kind="snapshot",
        workspace_path=materialized.workspace_path,
        state_path=plan.state_path,
        initial_snapshot_entries=materialized.initial_snapshot_entries,
        cleanup_on_success=workspace_cleanup_on_success(request.plan),
        workspace_state_payload=trusted_workspace_state_payload(plan.state_path),
        snapshot_cancel_requested=_snapshot_cancel_requested(request),
    )


def _snapshot_cancel_requested(
    request: WorkspaceInvocationRequest,
) -> Callable[[], bool] | None:
    cancellation = request.setup_cancellation
    return cancellation.is_cancelled if cancellation is not None else None
