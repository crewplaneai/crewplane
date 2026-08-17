from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from crewplane.architecture.contracts import JsonObject
from crewplane.core.workflow.keywords import (
    ALLOWED_NODE_ARTIFACT_NAME_SET,
    RESERVED_RUN_ROOT_NAMES,
    ProviderRole,
)
from crewplane.version import SCHEMA_VERSION

from .secrets import FINGERPRINT_PAYLOAD_VERSION

if TYPE_CHECKING:
    from .models import (
        DependencyEdge,
        PreflightExecutionNode,
        PreflightExecutionPlan,
        RenderPlan,
        WorkspaceFileLocator,
        WorkspaceSelectionRecord,
    )

CURRENT_FINGERPRINT_METADATA_FIELD = "payload_version"
CURRENT_RUNTIME_CONFIG_SCHEMA_FIELD = "schema_version"
CURRENT_VALUE_FINGERPRINT_VERSION_FIELD = "fingerprint_payload_version"
LEGACY_FINGERPRINT_METADATA_FIELDS = {"schema_version"}
LEGACY_RUNTIME_CONFIG_SCHEMA_FIELDS = {
    "config_schema_version",
    "workflow_schema_version",
}
LEGACY_VALUE_FINGERPRINT_VERSION_FIELDS = {"fingerprint_schema_version"}
RESERVED_STAGE_ROOTS = frozenset((*RESERVED_RUN_ROOT_NAMES, "preflight"))


def validate_supported_plan_schema_version(value: str) -> str:
    if value != SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported preflight plan schema version '{value}'. "
            f"Expected '{SCHEMA_VERSION}'."
        )
    return value


def validate_current_execution_plan_shape(
    runtime_config_snapshot: JsonObject,
    fingerprint_metadata: JsonObject,
    value_fingerprints: list[dict[str, str]],
    nodes: Sequence[object],
) -> None:
    _validate_current_runtime_snapshot_shape(runtime_config_snapshot)
    _validate_current_fingerprint_metadata_shape(fingerprint_metadata)
    _validate_current_value_fingerprint_shape(value_fingerprints)
    _validate_persisted_node_contracts(nodes)


def validate_execution_plan_semantics(plan: PreflightExecutionPlan) -> None:
    """Reject persisted plans whose cross-record relationships are impossible."""

    nodes_by_id = _unique_records(plan.nodes, "id", "node")
    _validate_execution_order(plan.execution_order, nodes_by_id)
    render_plans_by_id = _unique_records(
        plan.render_plans,
        "render_plan_id",
        "render plan",
    )
    _validate_node_relationships(plan, nodes_by_id, render_plans_by_id)
    _validate_dependency_graph(plan, nodes_by_id)
    _validate_render_plan_ownership(plan, render_plans_by_id)
    _validate_artifact_contract_uniqueness(plan.nodes)
    _validate_workspace_locator_relationships(
        plan,
        nodes_by_id,
        render_plans_by_id,
    )


def _unique_records[RecordT](
    records: Sequence[RecordT],
    field_name: str,
    label: str,
) -> dict[str, RecordT]:
    indexed: dict[str, RecordT] = {}
    for record in records:
        key = getattr(record, field_name, None)
        if not isinstance(key, str) or not key.strip():
            raise ValueError(
                f"Persisted {label} records require nonblank {field_name}."
            )
        if key in indexed:
            raise ValueError(f"Persisted plan contains duplicate {label} '{key}'.")
        indexed[key] = record
    return indexed


def _validate_execution_order(
    execution_order: list[str],
    nodes_by_id: dict[str, PreflightExecutionNode],
) -> None:
    if len(execution_order) != len(set(execution_order)):
        raise ValueError("Persisted plan execution_order contains duplicate nodes.")
    if set(execution_order) != set(nodes_by_id):
        raise ValueError(
            "Persisted plan execution_order must contain every node exactly once."
        )


def _validate_node_relationships(
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
        _validate_workspace_source_lineage(node, nodes_by_id, node_order)
        _validate_node_consensus_policy(node, snapshot_policy)
        _validate_provider_task_ids(node)


def _validate_dependency_graph(
    plan: PreflightExecutionPlan,
    nodes_by_id: dict[str, PreflightExecutionNode],
) -> None:
    node_order = {node_id: index for index, node_id in enumerate(plan.execution_order)}
    graph_dependencies = {node_id: set[str]() for node_id in nodes_by_id}
    seen_edges: set[tuple[str, str, str | None]] = set()
    for edge in plan.dependency_graph:
        _validate_dependency_edge(edge, nodes_by_id, node_order)
        edge_key = (edge.source_node, edge.target_node, edge.artifact_name)
        if edge_key in seen_edges:
            raise ValueError(
                "Persisted dependency graph contains duplicate edge "
                f"'{edge.source_node}' -> '{edge.target_node}'."
            )
        seen_edges.add(edge_key)
        graph_dependencies[edge.target_node].add(edge.source_node)

    _validate_runtime_locator_dependencies(plan, seen_edges)

    for node in plan.nodes:
        node_dependencies = set(node.dependencies)
        if graph_dependencies[node.id] != node_dependencies:
            raise ValueError(
                f"Persisted node '{node.id}' dependencies disagree with the "
                "compiled dependency graph."
            )


def _validate_runtime_locator_dependencies(
    plan: PreflightExecutionPlan,
    dependency_edges: set[tuple[str, str, str | None]],
) -> None:
    for render_plan in plan.render_plans:
        if render_plan.node_id is None:
            continue
        for stream in render_plan.streams:
            for fragment in stream.fragments:
                if fragment.kind != "runtime_locator_lookup":
                    continue
                locator = fragment.locator
                if locator is None:
                    continue
                dependency_edge = (
                    locator["node_id"],
                    render_plan.node_id,
                    locator["artifact_name"],
                )
                if dependency_edge not in dependency_edges:
                    raise ValueError(
                        f"Persisted render plan '{render_plan.render_plan_id}' "
                        f"contains runtime locator '{locator['node_id']}."
                        f"{locator['artifact_name']}' without a matching dependency "
                        "edge."
                    )


def _validate_dependency_edge(
    edge: DependencyEdge,
    nodes_by_id: dict[str, PreflightExecutionNode],
    node_order: dict[str, int],
) -> None:
    if not edge.source_node.strip() or not edge.target_node.strip():
        raise ValueError("Persisted dependency graph node ids cannot be blank.")
    if edge.source_node not in nodes_by_id:
        raise ValueError(
            "Persisted dependency graph references unknown source node "
            f"'{edge.source_node}'."
        )
    if edge.target_node not in nodes_by_id:
        raise ValueError(
            "Persisted dependency graph references unknown target node "
            f"'{edge.target_node}'."
        )
    if node_order[edge.source_node] >= node_order[edge.target_node]:
        raise ValueError(
            "Persisted dependency graph edge must point from an upstream node: "
            f"'{edge.source_node}' -> '{edge.target_node}'."
        )
    if edge.artifact_name not in {None, *ALLOWED_NODE_ARTIFACT_NAME_SET}:
        raise ValueError(
            f"Persisted dependency graph contains unknown artifact "
            f"'{edge.artifact_name}'."
        )
    if (
        edge.artifact_name is not None
        and edge.artifact_name.startswith("findings")
        and not nodes_by_id[edge.source_node].findings
    ):
        raise ValueError(
            f"Persisted dependency graph requests findings from node "
            f"'{edge.source_node}', which does not publish findings."
        )
    expected_artifact_key = edge.artifact_name
    expected_target_locator = (
        edge.source_node
        if edge.artifact_name is None
        else f"{edge.source_node}.{edge.artifact_name}"
    )
    if edge.artifact_key != expected_artifact_key:
        raise ValueError("Persisted dependency graph artifact_key is inconsistent.")
    if edge.target_locator != expected_target_locator:
        raise ValueError("Persisted dependency graph target_locator is inconsistent.")


def _validate_node_execution_policy(node: PreflightExecutionNode) -> None:
    policy = node.execution_policy
    provider_roles = [provider.role for provider in node.provider_records]
    if node.mode == "input":
        _validate_input_execution_policy(node)
        return
    if node.mode == "parallel":
        if policy.depth is not None or policy.audit_rounds is not None:
            raise ValueError(
                f"Persisted parallel node '{node.id}' cannot define depth or "
                "audit_rounds."
            )
        if policy.review_starts_with != ProviderRole.EXECUTOR:
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
        return

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
        or policy.review_starts_with != ProviderRole.EXECUTOR
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


def _validate_render_plan_ownership(
    plan: PreflightExecutionPlan,
    render_plans_by_id: dict[str, RenderPlan],
) -> None:
    referenced_ids = {
        node.render_plan_id for node in plan.nodes if node.render_plan_id is not None
    }
    unreferenced_ids = sorted(set(render_plans_by_id).difference(referenced_ids))
    if unreferenced_ids:
        raise ValueError(
            "Persisted render plans must be owned by exactly one node: "
            + ", ".join(unreferenced_ids)
        )


def _validate_workspace_source_lineage(
    node: PreflightExecutionNode,
    nodes_by_id: dict[str, PreflightExecutionNode],
    node_order: dict[str, int],
) -> None:
    policy = node.workspace_policy
    if policy is None:
        return
    _validate_declared_workspace_source(node, nodes_by_id, node_order)
    _validate_direct_worktree_dependencies(node, nodes_by_id)
    if not _is_lineage_worktree_policy(policy):
        return
    expected_source = _latest_same_worktree_source(node, nodes_by_id, node_order)
    if expected_source is None:
        if policy.source_kind != "project" or policy.source_node_id is not None:
            raise ValueError(
                f"Persisted node '{node.id}' must use project workspace source "
                "without a same-worktree lineage ancestor."
            )
        return
    if policy.source_kind != "node" or policy.source_node_id != expected_source:
        raise ValueError(
            f"Persisted node '{node.id}' must use latest same-worktree lineage "
            f"source '{expected_source}'."
        )


def _validate_declared_workspace_source(
    node: PreflightExecutionNode,
    nodes_by_id: dict[str, PreflightExecutionNode],
    node_order: dict[str, int],
) -> None:
    policy = node.workspace_policy
    if policy is None or policy.source_kind != "node":
        return
    source_node_id = policy.source_node_id
    if source_node_id is None or source_node_id not in nodes_by_id:
        raise ValueError(
            f"Persisted node '{node.id}' workspace policy references an unknown "
            "source node."
        )
    if source_node_id not in _ancestor_node_ids(node, nodes_by_id):
        raise ValueError(
            f"Persisted node '{node.id}' workspace source '{source_node_id}' must "
            "be an upstream dependency."
        )
    if node_order[source_node_id] >= node_order[node.id]:
        raise ValueError(
            f"Persisted node '{node.id}' workspace source '{source_node_id}' is not "
            "upstream."
        )
    source_policy = nodes_by_id[source_node_id].workspace_policy
    if (
        source_policy is None
        or not source_policy.enabled
        or not source_policy.lineage_producer
    ):
        raise ValueError(
            f"Persisted node '{node.id}' workspace source '{source_node_id}' must "
            "be an enabled lineage producer."
        )
    if source_policy.logical_worktree_name != policy.logical_worktree_name:
        raise ValueError(
            f"Persisted node '{node.id}' workspace source '{source_node_id}' must "
            "use the same logical worktree."
        )


def _validate_direct_worktree_dependencies(
    node: PreflightExecutionNode,
    nodes_by_id: dict[str, PreflightExecutionNode],
) -> None:
    policy = node.workspace_policy
    if policy is None or not _is_lineage_worktree_policy(policy):
        return
    for dependency_id in node.dependencies:
        dependency = nodes_by_id.get(dependency_id)
        dependency_policy = (
            dependency.workspace_policy if dependency is not None else None
        )
        if dependency_policy is None or not _is_lineage_worktree_policy(
            dependency_policy
        ):
            continue
        if dependency_policy.logical_worktree_name != policy.logical_worktree_name:
            raise ValueError(
                f"Persisted node '{node.id}' directly depends on lineage producer "
                f"'{dependency_id}' from a different logical worktree; dependencies "
                "between worktrees must use the same logical worktree."
            )


def _latest_same_worktree_source(
    node: PreflightExecutionNode,
    nodes_by_id: dict[str, PreflightExecutionNode],
    node_order: dict[str, int],
) -> str | None:
    policy = node.workspace_policy
    if policy is None:
        return None
    candidates: list[str] = []
    for node_id in _ancestor_node_ids(node, nodes_by_id):
        candidate_policy = nodes_by_id[node_id].workspace_policy
        if candidate_policy is None or not _is_lineage_worktree_policy(
            candidate_policy
        ):
            continue
        if candidate_policy.logical_worktree_name == policy.logical_worktree_name:
            candidates.append(node_id)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda node_id: (
            len(_ancestor_node_ids(nodes_by_id[node_id], nodes_by_id)),
            node_order[node_id],
        ),
    )


def _ancestor_node_ids(
    node: PreflightExecutionNode,
    nodes_by_id: dict[str, PreflightExecutionNode],
) -> set[str]:
    pending = list(node.dependencies)
    ancestors: set[str] = set()
    while pending:
        dependency_id = pending.pop()
        if dependency_id in ancestors:
            continue
        ancestors.add(dependency_id)
        dependency = nodes_by_id.get(dependency_id)
        if dependency is not None:
            pending.extend(dependency.dependencies)
    return ancestors.intersection(nodes_by_id)


def _is_lineage_worktree_policy(
    policy: WorkspaceSelectionRecord | None,
) -> bool:
    return bool(
        policy is not None
        and policy.enabled
        and policy.declaration_kind == "worktree"
        and policy.lineage_producer
    )


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
            f"Persisted node '{_node_id(node)}' defines consensus policy outside a "
            "sequential review loop."
        )
    if uses_review_loop and node_policy is None:
        raise ValueError(
            f"Persisted review-loop node '{_node_id(node)}' is missing consensus policy."
        )
    if uses_review_loop and node_policy != snapshot_policy:
        raise ValueError(
            f"Persisted review-loop node '{_node_id(node)}' consensus policy conflicts "
            "with the signed runtime snapshot."
        )


def _validate_artifact_contract_uniqueness(
    nodes: Sequence[PreflightExecutionNode],
) -> None:
    occupied: dict[tuple[str, str], str] = {}
    for node in nodes:
        contract = node.artifact_contract
        if contract.stage_path is None:
            raise ValueError(
                f"Persisted node '{_node_id(node)}' is missing its stage locator."
            )
        stage_parts = contract.stage_path.split("/")
        if stage_parts[0] in RESERVED_STAGE_ROOTS:
            raise ValueError(
                f"Persisted node '{_node_id(node)}' uses reserved stage root "
                f"'{stage_parts[0]}'."
            )
        if contract.result_path != contract.output_path:
            raise ValueError(
                f"Persisted node '{_node_id(node)}' has conflicting output locators."
            )
        if bool(node.findings) != (contract.findings_path is not None):
            raise ValueError(
                f"Persisted node '{_node_id(node)}' has an inconsistent findings locator."
            )
        expected_log_path = f"{contract.stage_path}/logs"
        if contract.log_path != expected_log_path:
            raise ValueError(
                f"Persisted node '{_node_id(node)}' has a log locator outside its stage."
            )
        for root, field_name in (
            ("stages", "stage_path"),
            ("results", "output_path"),
            ("results", "findings_path"),
        ):
            path = getattr(contract, field_name)
            if path is None:
                continue
            existing = next(
                (
                    (existing_key, existing_node)
                    for existing_key, existing_node in occupied.items()
                    if existing_key[0] == root and _paths_overlap(existing_key[1], path)
                ),
                None,
            )
            if existing is not None:
                raise ValueError(
                    f"Persisted nodes '{existing[1]}' and '{_node_id(node)}' "
                    f"have overlapping {root} artifact locators: "
                    f"'{existing[0][1]}' and '{path}'."
                )
            occupied[(root, path)] = _node_id(node)


def _paths_overlap(left: str, right: str) -> bool:
    left_path = Path(left)
    right_path = Path(right)
    return (
        left_path == right_path
        or left_path.is_relative_to(right_path)
        or right_path.is_relative_to(left_path)
    )


def _validate_workspace_locator_relationships(
    plan: PreflightExecutionPlan,
    nodes_by_id: dict[str, PreflightExecutionNode],
    render_plans_by_id: dict[str, RenderPlan],
) -> None:
    locators_by_id = _unique_records(
        plan.workspace_file_locators,
        "locator_id",
        "workspace file locator",
    )
    _unique_records(
        plan.workspace_file_locators,
        "occurrence_id",
        "workspace file occurrence",
    )
    unknown_node_locators = sorted(
        locator.locator_id
        for locator in plan.workspace_file_locators
        if locator.node_id not in nodes_by_id
    )
    if unknown_node_locators:
        raise ValueError(
            "Persisted workspace locators reference unknown nodes: "
            + ", ".join(unknown_node_locators)
        )
    referenced: dict[str, int] = {locator_id: 0 for locator_id in locators_by_id}
    for node in plan.nodes:
        _validate_node_workspace_locator(node, locators_by_id, referenced)
        if node.render_plan_id is None:
            continue
        render_plan = render_plans_by_id[node.render_plan_id]
        for stream in render_plan.streams:
            for fragment in stream.fragments:
                if fragment.kind != "workspace_file_locator":
                    continue
                locator_id = (fragment.locator or {}).get("locator_id")
                locator = locators_by_id.get(locator_id or "")
                if locator is None:
                    raise ValueError(
                        f"Persisted node '{node.id}' references unknown workspace "
                        f"locator '{locator_id}'."
                    )
                if locator.node_id != node.id:
                    raise ValueError("Workspace locator node identity is inconsistent.")
                expected_target = f"{stream.target_role.value}_prompt"
                if locator.target.value != expected_target:
                    raise ValueError(
                        "Workspace locator target conflicts with its stream."
                    )
                referenced[locator_id or ""] += 1
    unreferenced = sorted(
        locator_id for locator_id, count in referenced.items() if count != 1
    )
    if unreferenced:
        raise ValueError(
            "Persisted workspace locators must each have exactly one owner: "
            + ", ".join(unreferenced)
        )


def _validate_node_workspace_locator(
    node: PreflightExecutionNode,
    locators_by_id: dict[str, WorkspaceFileLocator],
    referenced: dict[str, int],
) -> None:
    input_locator_id = node.input_workspace_file_locator_id
    if input_locator_id is not None:
        locator = locators_by_id.get(input_locator_id)
        if locator is None or locator.node_id != _node_id(node):
            raise ValueError(
                f"Persisted input node '{_node_id(node)}' references an invalid "
                "workspace locator."
            )
        if locator.target.value != "input_output":
            raise ValueError("Input node workspace locator has the wrong target.")
        referenced[input_locator_id] += 1
    node_locators = [
        locator
        for locator in locators_by_id.values()
        if locator.node_id == _node_id(node)
    ]
    managed_locators = [
        locator for locator in node_locators if locator.target.value != "input_output"
    ]
    if not managed_locators:
        return
    workspace_policy = node.workspace_policy
    if workspace_policy is None or not workspace_policy.enabled:
        raise ValueError(
            f"Persisted node '{_node_id(node)}' has workspace locators without an "
            "enabled workspace policy."
        )
    for locator in managed_locators:
        expected_source_class = _expected_workspace_locator_source_class(
            workspace_policy,
            locator,
        )
        if locator.source_class.value != expected_source_class:
            raise ValueError(
                f"Persisted node '{_node_id(node)}' workspace locator "
                f"'{locator.locator_id}' source class conflicts with its target "
                "and workspace lineage."
            )


def _expected_workspace_locator_source_class(
    workspace_policy: WorkspaceSelectionRecord,
    locator: WorkspaceFileLocator,
) -> str:
    if workspace_policy.declaration_kind != "worktree":
        return "project_initial"
    if (
        locator.target.value == "reviewer_prompt"
        or workspace_policy.source_kind == "node"
    ):
        return "runtime_dynamic"
    return "project_initial_then_candidate"


def _validate_current_runtime_snapshot_shape(snapshot: JsonObject) -> None:
    _reject_legacy_plan_fields(
        snapshot,
        LEGACY_RUNTIME_CONFIG_SCHEMA_FIELDS,
        "runtime config snapshot",
    )
    if CURRENT_RUNTIME_CONFIG_SCHEMA_FIELD not in snapshot:
        raise ValueError(
            "Preflight plan runtime config snapshot must include "
            f"'{CURRENT_RUNTIME_CONFIG_SCHEMA_FIELD}'."
        )
    if snapshot[CURRENT_RUNTIME_CONFIG_SCHEMA_FIELD] != SCHEMA_VERSION:
        raise ValueError(
            "Preflight plan runtime config snapshot schema_version must be "
            f"'{SCHEMA_VERSION}'."
        )
    for integration_name in ("invoker", "artifacts", "ui"):
        integration_payload = snapshot.get(integration_name)
        if isinstance(integration_payload, dict):
            _reject_legacy_plan_fields(
                integration_payload,
                {"api_version"},
                f"runtime config '{integration_name}' integration",
            )


def _validate_current_fingerprint_metadata_shape(metadata: JsonObject) -> None:
    _reject_legacy_plan_fields(
        metadata,
        LEGACY_FINGERPRINT_METADATA_FIELDS,
        "fingerprint metadata",
    )
    if CURRENT_FINGERPRINT_METADATA_FIELD not in metadata:
        raise ValueError(
            "Preflight plan fingerprint metadata must include "
            f"'{CURRENT_FINGERPRINT_METADATA_FIELD}'."
        )
    if metadata[CURRENT_FINGERPRINT_METADATA_FIELD] != FINGERPRINT_PAYLOAD_VERSION:
        raise ValueError(
            "Preflight plan fingerprint metadata payload_version must be "
            f"'{FINGERPRINT_PAYLOAD_VERSION}'."
        )


def _validate_current_value_fingerprint_shape(
    records: list[dict[str, str]],
) -> None:
    for index, record in enumerate(records):
        _reject_legacy_plan_fields(
            record,
            LEGACY_VALUE_FINGERPRINT_VERSION_FIELDS,
            f"value fingerprint at index {index}",
        )
        if CURRENT_VALUE_FINGERPRINT_VERSION_FIELD not in record:
            raise ValueError(
                "Preflight plan value fingerprint at index "
                f"{index} must include '{CURRENT_VALUE_FINGERPRINT_VERSION_FIELD}'."
            )
        if (
            record[CURRENT_VALUE_FINGERPRINT_VERSION_FIELD]
            != FINGERPRINT_PAYLOAD_VERSION
        ):
            raise ValueError(
                "Preflight plan value fingerprint at index "
                f"{index} payload version must be '{FINGERPRINT_PAYLOAD_VERSION}'."
            )


def _validate_persisted_node_contracts(nodes: Sequence[object]) -> None:
    for node in nodes:
        if getattr(node, "mode", None) == "input":
            _validate_persisted_input_node_contract(node)
            continue
        _validate_persisted_provider_node_contract(node)


def _validate_persisted_input_node_contract(node: object) -> None:
    input_sources = [
        field
        for field in ("input_content_ref", "input_workspace_file_locator_id")
        if str(getattr(node, field, "") or "").strip()
    ]
    if len(input_sources) != 1:
        raise ValueError(
            "Persisted input preflight node "
            f"'{_node_id(node)}' must define exactly one input source reference."
        )


def _validate_persisted_provider_node_contract(node: object) -> None:
    if not str(getattr(node, "render_plan_id", "") or "").strip():
        raise ValueError(
            "Persisted provider preflight node "
            f"'{_node_id(node)}' must define render_plan_id."
        )
    if not getattr(node, "provider_records", ()):
        raise ValueError(
            "Persisted provider preflight node "
            f"'{_node_id(node)}' must define at least one provider record."
        )


def _node_id(node: object) -> str:
    value = getattr(node, "id", "<unknown>")
    return value if isinstance(value, str) else "<unknown>"


def _reject_legacy_plan_fields(
    payload: dict[str, object],
    legacy_fields: set[str],
    payload_label: str,
) -> None:
    present_fields = sorted(field for field in legacy_fields if field in payload)
    if not present_fields:
        return
    joined_fields = ", ".join(present_fields)
    raise ValueError(
        f"Unsupported legacy preflight plan {payload_label} field(s): {joined_fields}."
    )
