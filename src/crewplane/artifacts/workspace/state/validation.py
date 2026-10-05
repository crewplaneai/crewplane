from __future__ import annotations

from collections.abc import Mapping

from crewplane.core.preflight.models import (
    PreflightExecutionNode,
    PreflightExecutionPlan,
)
from crewplane.core.preflight.runtime_config.workspace import (
    invoker_workspace_descriptor,
    requires_controlled_child_environment,
)
from crewplane.core.preflight.workspace.models import is_lineage_worktree
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workspace.policy import WorkspaceMaterialization
from crewplane.version import SCHEMA_VERSION

from ...run_history import RunHistoryRecord
from ..rendered_file_validation import (
    provider_rendered_workspace_files_match,
)
from ..source_validation import workspace_invocation_source_matches
from . import materialization_results
from .contracts import workspace_state_contract_is_valid
from .expected_set import workspace_state_payloads_match_expected_set
from .fields import WorkspaceArtifactRoot
from .fields import (
    bool_field_matches as _bool_field_matches,
)
from .fields import (
    mapping_value as _mapping,
)
from .invocations import (
    ExpectedWorkspaceInvocation,
    WorkspaceStateStatus,
    expected_failed_workspace_invocations,
    expected_workspace_invocations,
    failed_workspace_state_payloads,
    payload_matches_expected_invocation,
    workspace_state_payloads,
)
from .ref_contracts import is_discarded_lineage


def workspace_node_state_is_valid(
    source: RunHistoryRecord,
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
) -> bool:
    """Validate persisted invocation evidence for a node's workspace."""
    policy = node.workspace_policy
    if policy is None or not policy.enabled:
        return True
    expected_invocations = expected_workspace_invocations(source, node)
    expected_failed_invocations = expected_failed_workspace_invocations(source, node)
    if not expected_invocations and not expected_failed_invocations:
        return False
    if not _parallel_workspace_outputs_are_complete(
        node,
        expected_invocations,
        expected_failed_invocations,
    ):
        return False
    if _has_undiscarded_failed_lineage(source, node):
        return False
    if not _successful_workspace_invocations_are_valid(
        source, plan, node, expected_invocations
    ):
        return False
    return _failed_workspace_invocations_are_valid(
        source, plan, node, expected_failed_invocations
    )


def _has_undiscarded_failed_lineage(
    source: RunHistoryRecord,
    node: PreflightExecutionNode,
) -> bool:
    return is_lineage_worktree(node.workspace_policy) and any(
        payload.get("role") == ProviderRole.EXECUTOR
        and not is_discarded_lineage(payload)
        for payload in failed_workspace_state_payloads(source, node)
    )


def _successful_workspace_invocations_are_valid(
    source: RunHistoryRecord,
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    expected_invocations: tuple[ExpectedWorkspaceInvocation, ...],
) -> bool:
    state_payloads = workspace_state_payloads(source, node)
    if bool(expected_invocations) != bool(state_payloads):
        return False
    if not all(
        _provider_workspace_state_is_valid(source, plan, node, payload)
        for payload in state_payloads
    ):
        return False
    return workspace_state_payloads_match_expected_set(
        state_payloads, expected_invocations
    )


def _parallel_workspace_outputs_are_complete(
    node: PreflightExecutionNode,
    expected_invocations: tuple[ExpectedWorkspaceInvocation, ...],
    expected_failed_invocations: tuple[ExpectedWorkspaceInvocation, ...],
) -> bool:
    if node.mode != "parallel":
        return True
    expected_count = sum(
        1
        for provider in node.provider_records
        if provider.role == ProviderRole.EXECUTOR
    )
    actual_count = len(expected_invocations) + len(expected_failed_invocations)
    return actual_count == expected_count


def _failed_workspace_invocations_are_valid(
    source: RunHistoryRecord,
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    expected_invocations: tuple[ExpectedWorkspaceInvocation, ...],
) -> bool:
    payloads = failed_workspace_state_payloads(source, node)
    if len(payloads) != len(expected_invocations):
        return False
    if not all(
        _failed_provider_workspace_state_is_valid(source, plan, node, payload)
        for payload in payloads
    ):
        return False
    for expected in expected_invocations:
        matches = [
            payload
            for payload in payloads
            if payload_matches_expected_invocation(payload, expected)
        ]
        if len(matches) != 1:
            return False
    return True


def _provider_workspace_state_is_valid(
    source: RunHistoryRecord,
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    payload: dict[str, object],
) -> bool:
    policy = node.workspace_policy
    if policy is None or not workspace_state_contract_is_valid(
        payload,
        "duplicate_skip",
    ):
        return False
    if not (
        _workspace_state_header_matches(source, plan, node, payload)
        and _workspace_state_policy_matches(policy.model_dump(mode="json"), payload)
        and _workspace_invocation_context_matches(source, plan, node, payload)
    ):
        return False
    workspace = _mapping(payload.get("workspace"))
    if not _workspace_placement_matches(workspace, policy.materialization):
        return False
    return materialization_results.workspace_materialization_result_matches(
        policy.materialization, source, plan, payload
    )


def checkpoint_invocation_is_valid(
    source: WorkspaceArtifactRoot,
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    payload: dict[str, object],
    source_matches: bool,
    run_id: str,
    run_key_name: str,
) -> bool:
    """Validate semantic invocation evidence without consulting live placement."""
    policy = node.workspace_policy
    failed = payload.get("status") == WorkspaceStateStatus.FAILED
    if policy is None or not workspace_state_contract_is_valid(
        payload, "checkpoint_failed" if failed else "checkpoint"
    ):
        return False
    workspace = _mapping(payload.get("workspace"))
    if not (
        _workspace_state_identity_matches(plan, node, payload, run_id, run_key_name)
        and _workspace_state_policy_matches(policy.model_dump(mode="json"), payload)
        and _checkpoint_invocation_context_matches(
            source,
            plan,
            node,
            payload,
            source_matches,
            WorkspaceStateStatus.FAILED if failed else WorkspaceStateStatus.SUCCEEDED,
        )
        and _checkpoint_placement_matches(workspace, policy.materialization, payload)
    ):
        return False
    return failed or materialization_results.workspace_materialization_result_matches(
        policy.materialization, source, plan, payload
    )


def _checkpoint_invocation_context_matches(
    source: WorkspaceArtifactRoot,
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    payload: dict[str, object],
    source_matches: bool,
    status: WorkspaceStateStatus,
) -> bool:
    return (
        _workspace_state_invoker_matches(plan, payload, status)
        and _workspace_state_git_matches(plan, payload)
        and source_matches
        and provider_rendered_workspace_files_match(plan, node, payload, source)
    )


def _workspace_placement_matches(
    workspace: Mapping[str, object],
    materialization: WorkspaceMaterialization,
) -> bool:
    return (
        workspace.get("materialization") == materialization
        and workspace.get("path") is None
        and workspace.get("effective_cwd") is None
    )


def _checkpoint_placement_matches(
    workspace: Mapping[str, object],
    materialization: WorkspaceMaterialization,
    payload: dict[str, object],
) -> bool:
    return _workspace_placement_matches(workspace, materialization) and all(
        _mapping(payload.get("execution")).get(key) is None
        for key in (
            "workspace_path",
            "effective_cwd",
            "cache_root",
            "checkout_root",
            "worktree_git_dir",
        )
    )


def _workspace_invocation_context_matches(
    source: RunHistoryRecord,
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    payload: dict[str, object],
    status: WorkspaceStateStatus = WorkspaceStateStatus.SUCCEEDED,
) -> bool:
    return (
        _workspace_state_invoker_matches(plan, payload, status)
        and _workspace_state_git_matches(plan, payload)
        and workspace_invocation_source_matches(source, plan, node, payload)
        and provider_rendered_workspace_files_match(plan, node, payload, source)
    )


def _failed_provider_workspace_state_is_valid(
    source: RunHistoryRecord,
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    payload: dict[str, object],
) -> bool:
    policy = node.workspace_policy
    if policy is None or not workspace_state_contract_is_valid(
        payload, "failed_invocation"
    ):
        return False
    workspace = _mapping(payload.get("workspace"))
    return (
        _workspace_state_context_matches(
            source, plan, node, payload, WorkspaceStateStatus.FAILED
        )
        and _workspace_state_policy_matches(policy.model_dump(mode="json"), payload)
        and _workspace_invocation_context_matches(
            source, plan, node, payload, WorkspaceStateStatus.FAILED
        )
        and _workspace_placement_matches(workspace, policy.materialization)
        and _bool_field_matches(workspace, "writable", policy.writable)
        and _bool_field_matches(
            workspace,
            "lineage_producer",
            policy.lineage_producer
            and payload.get("role") == ProviderRole.EXECUTOR
            and not is_discarded_lineage(payload),
        )
    )


def _workspace_state_git_matches(
    plan: PreflightExecutionPlan,
    payload: dict[str, object],
) -> bool:
    workspace_source = plan.workspace_source
    if workspace_source is None:
        return False
    git = _mapping(payload.get("git"))
    return (
        git.get("object_format") == workspace_source.object_format
        and git.get("repo_id") == workspace_source.repository_id
        and git.get("run_base_commit") == workspace_source.run_base_commit
        and git.get("source_tree") == workspace_source.source_tree
    )


def _workspace_state_header_matches(
    source: RunHistoryRecord,
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    payload: dict[str, object],
) -> bool:
    return _workspace_state_context_matches(
        source, plan, node, payload, WorkspaceStateStatus.SUCCEEDED
    )


def _workspace_state_context_matches(
    source: RunHistoryRecord,
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    payload: dict[str, object],
    status: WorkspaceStateStatus,
) -> bool:
    return (
        _workspace_state_identity_matches(
            plan, node, payload, source.manifest.run_id, source.manifest.run_key_name
        )
        and payload.get("status") == status
    )


def _workspace_state_identity_matches(
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    payload: dict[str, object],
    run_id: str,
    run_key_name: str,
) -> bool:
    return (
        payload.get("version") == SCHEMA_VERSION
        and payload.get("run_id") == run_id
        and payload.get("run_key_name") == run_key_name
        and payload.get("workflow_name") == plan.workflow_name
        and payload.get("workflow_signature") == plan.workflow_signature
        and payload.get("node_id") == node.id
    )


def _workspace_state_policy_matches(
    policy: dict[str, object],
    payload: dict[str, object],
) -> bool:
    return (
        payload.get("workspace_kind") == policy.get("declaration_kind")
        and payload.get("logical_worktree_name") == policy.get("logical_worktree_name")
        and payload.get("clean_start") == policy.get("clean_start")
        and payload.get("worktree_contract") == policy.get("worktree_contract")
    )


def _workspace_state_invoker_matches(
    plan: PreflightExecutionPlan,
    payload: dict[str, object],
    status: WorkspaceStateStatus = WorkspaceStateStatus.SUCCEEDED,
) -> bool:
    expected = invoker_workspace_descriptor(plan.runtime_config_snapshot)
    return (
        expected is not None
        and _mapping(payload.get("invoker")) == expected
        and _child_process_environment_matches(expected, payload, status)
    )


def _child_process_environment_matches(
    invoker: Mapping[str, object],
    payload: dict[str, object],
    status: WorkspaceStateStatus,
) -> bool:
    if not requires_controlled_child_environment(invoker):
        return True
    child_environment = _mapping(payload.get("child_process_environment"))
    return child_environment.get("required") is True and (
        isinstance(child_environment.get("applied"), bool)
        if status == WorkspaceStateStatus.FAILED
        else child_environment.get("applied") is True
    )
