"""Validate persisted preflight execution-plan contracts."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from crewplane.architecture.contracts import JsonObject
from crewplane.version import SCHEMA_VERSION

from . import (
    plan_contract_artifacts,
    plan_contract_dependencies,
    plan_contract_nodes,
    plan_contract_records,
    plan_contract_shape,
    plan_contract_workspace_locators,
)

if TYPE_CHECKING:
    from .models import PreflightExecutionPlan

__all__ = [
    "validate_current_execution_plan_shape",
    "validate_execution_plan_semantics",
    "validate_supported_plan_schema_version",
]


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
    plan_contract_shape.validate_current_runtime_snapshot_shape(runtime_config_snapshot)
    plan_contract_shape.validate_current_fingerprint_metadata_shape(
        fingerprint_metadata
    )
    plan_contract_shape.validate_current_value_fingerprint_shape(value_fingerprints)
    plan_contract_shape.validate_persisted_node_contracts(nodes)


def validate_execution_plan_semantics(plan: PreflightExecutionPlan) -> None:
    """Reject persisted plans whose cross-record relationships are impossible."""

    nodes_by_id = plan_contract_records.unique_records(plan.nodes, "id", "node")
    plan_contract_records.validate_execution_order(plan.execution_order, nodes_by_id)
    render_plans_by_id = plan_contract_records.unique_records(
        plan.render_plans,
        "render_plan_id",
        "render plan",
    )
    plan_contract_nodes.validate_node_relationships(
        plan,
        nodes_by_id,
        render_plans_by_id,
    )
    plan_contract_dependencies.validate_dependency_graph(plan, nodes_by_id)
    plan_contract_records.validate_render_plan_ownership(plan, render_plans_by_id)
    plan_contract_artifacts.validate_artifact_contract_uniqueness(plan.nodes)
    plan_contract_workspace_locators.validate_workspace_locator_relationships(
        plan,
        nodes_by_id,
        render_plans_by_id,
    )
