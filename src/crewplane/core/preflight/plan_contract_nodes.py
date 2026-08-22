"""Validate persisted preflight execution node contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from crewplane.architecture.contracts import JsonObject
from crewplane.core.workflow.keywords import ProviderRole

from . import plan_contract_records, plan_contract_workspace_lineage

if TYPE_CHECKING:
    from .models import PreflightExecutionNode, PreflightExecutionPlan, RenderPlan


def validate_node_relationships(
    plan: PreflightExecutionPlan,
    nodes_by_id: dict[str, PreflightExecutionNode],
    render_plans_by_id: dict[str, RenderPlan],
) -> None:
    node_order = {node_id: index for index, node_id in enumerate(plan.execution_order)}
    snapshot_policy = _snapshot_consensus_policy(plan.runtime_config_snapshot)
    for node in plan.nodes:
        if len(node.dependencies) != len(set(node.dependencies)):
            raise ValueError(
                f"Persisted node '{node.id}' contains duplicate dependencies."
            )
        if node.render_plan_id is not None:
            render_plan = render_plans_by_id.get(node.render_plan_id)
            if render_plan is None or render_plan.node_id != node.id:
                raise ValueError(
                    f"Persisted node '{node.id}' has an invalid render_plan_id."
                )
        _validate_node_execution_policy(node)
        _validate_node_concurrency_policy(node, plan.runtime_config_snapshot)
        plan_contract_workspace_lineage.validate_workspace_source_lineage(
            node,
            nodes_by_id,
            node_order,
        )
        _validate_node_consensus_policy(node, snapshot_policy)
        _validate_provider_task_ids(node)


def _validate_node_execution_policy(node: PreflightExecutionNode) -> None:
    provider_roles = [provider.role for provider in node.provider_records]
    if node.mode == "input":
        _validate_input_execution_policy(node)
        return
    if node.mode == "parallel":
        _validate_parallel_execution_policy(node, provider_roles)
        return

    _validate_sequential_execution_policy(node, provider_roles)


def _validate_parallel_execution_policy(
    node: PreflightExecutionNode,
    provider_roles: list[ProviderRole],
) -> None:
    policy = node.execution_policy
    if policy.depth is not None or policy.audit_rounds is not None:
        raise ValueError(
            f"Persisted parallel node '{node.id}' cannot define depth or audit_rounds."
        )
    if policy.review_starts_with != "executor":
        raise ValueError(
            f"Persisted parallel node '{node.id}' must start with executors."
        )
    if policy.consensus_on_exhaustion is not None:
        raise ValueError(
            f"Persisted parallel node '{node.id}' cannot define consensus policy."
        )
    if any(role != ProviderRole.EXECUTOR for role in provider_roles):
        raise ValueError(
            f"Persisted parallel node '{node.id}' cannot contain reviewers."
        )
    if policy.failure_threshold is not None and policy.failure_threshold >= len(
        provider_roles
    ):
        raise ValueError(
            f"Persisted parallel node '{node.id}' failure threshold must be "
            "less than its provider count."
        )


def _validate_sequential_execution_policy(
    node: PreflightExecutionNode,
    provider_roles: list[ProviderRole],
) -> None:
    policy = node.execution_policy

    if policy.failure_threshold is not None:
        raise ValueError(
            f"Persisted sequential node '{node.id}' cannot define failure threshold."
        )
    if len(provider_roles) == 1:
        if provider_roles[0] != ProviderRole.EXECUTOR:
            raise ValueError(
                f"Persisted single-provider node '{node.id}' must use an executor."
            )
        if policy.audit_rounds is not None:
            raise ValueError(
                f"Persisted single-provider node '{node.id}' cannot define "
                "audit_rounds."
            )
        return

    _validate_review_loop_provider_order(node, provider_roles)


def _validate_review_loop_provider_order(
    node: PreflightExecutionNode,
    provider_roles: list[ProviderRole],
) -> None:
    reviewer_started = False
    for role in provider_roles:
        if role == ProviderRole.REVIEWER:
            reviewer_started = True
        elif reviewer_started:
            raise ValueError(
                f"Persisted review-loop node '{node.id}' must group executors "
                "before reviewers."
            )
    if provider_roles[0] != ProviderRole.EXECUTOR or not reviewer_started:
        raise ValueError(
            f"Persisted review-loop node '{node.id}' must contain executor and "
            "reviewer providers in that order."
        )


def _validate_input_execution_policy(node: PreflightExecutionNode) -> None:
    policy = node.execution_policy
    if node.findings or node.dependencies:
        raise ValueError(
            f"Persisted input node '{node.id}' cannot define findings or dependencies."
        )
    if (
        policy.depth is not None
        or policy.audit_rounds is not None
        or policy.review_starts_with != "executor"
        or policy.continue_on_failure
        or policy.failure_threshold is not None
        or policy.token_budget is not None
        or policy.consensus_on_exhaustion is not None
        or policy.retry_policy.model_dump()
    ):
        raise ValueError(
            f"Persisted input node '{node.id}' contains provider execution policy."
        )


def _validate_node_concurrency_policy(
    node: PreflightExecutionNode,
    runtime_config_snapshot: JsonObject,
) -> None:
    execution = runtime_config_snapshot.get("execution")
    if execution is None:
        execution = {}
    elif not isinstance(execution, dict):
        raise ValueError(
            "Persisted plan runtime config snapshot must contain an execution object."
        )
    expected = {
        field_name: execution[field_name]
        for field_name in (
            "max_concurrent_nodes",
            "max_parallel_invocations",
        )
        if execution.get(field_name) is not None
    }
    actual = node.execution_policy.concurrency_policy.model_dump(exclude_none=True)
    if actual != expected:
        raise ValueError(
            f"Persisted node '{node.id}' concurrency policy conflicts with the "
            "runtime config snapshot."
        )


def _validate_provider_task_ids(node: PreflightExecutionNode) -> None:
    task_ids: set[str] = set()
    for provider in node.provider_records:
        for field_name in (
            "provider",
            "agent_config_key",
            "invoker_alias",
            "agent_config_signature",
            "invoker_config_signature",
        ):
            value = getattr(provider, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"Persisted node '{node.id}' provider {field_name} cannot be blank."
                )
        if not provider.task_id.strip():
            raise ValueError(
                f"Persisted node '{node.id}' provider task_id cannot be blank."
            )
        if provider.task_id in task_ids:
            raise ValueError(
                f"Persisted node '{node.id}' contains duplicate provider task_id "
                f"'{provider.task_id}'."
            )
        task_ids.add(provider.task_id)


def _snapshot_consensus_policy(snapshot: JsonObject) -> object:
    execution = snapshot.get("execution")
    if not isinstance(execution, dict):
        return None
    return execution.get("sequential_consensus_on_exhaustion")


def _validate_node_consensus_policy(
    node: PreflightExecutionNode,
    snapshot_policy: object,
) -> None:
    execution_policy = node.execution_policy
    node_policy = execution_policy.consensus_on_exhaustion
    providers = node.provider_records
    uses_review_loop = node.mode == "sequential" and any(
        provider.role == "reviewer" for provider in providers
    )
    if not uses_review_loop and node_policy is not None:
        raise ValueError(
            f"Persisted node '{plan_contract_records.node_id(node)}' defines "
            "consensus policy outside a sequential review loop."
        )
    if uses_review_loop and node_policy is None:
        raise ValueError(
            f"Persisted review-loop node '{plan_contract_records.node_id(node)}' "
            "is missing consensus policy."
        )
    if uses_review_loop and node_policy != snapshot_policy:
        raise ValueError(
            f"Persisted review-loop node '{plan_contract_records.node_id(node)}' "
            "consensus policy conflicts with the signed runtime snapshot."
        )
