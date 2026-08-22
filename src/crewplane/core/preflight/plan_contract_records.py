"""Validate persisted preflight plan record identity and ownership."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import PreflightExecutionNode, PreflightExecutionPlan, RenderPlan


def node_id(node: object) -> str:
    value = getattr(node, "id", "<unknown>")
    return value if isinstance(value, str) else "<unknown>"


def unique_records[RecordT](
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


def validate_execution_order(
    execution_order: list[str],
    nodes_by_id: dict[str, PreflightExecutionNode],
) -> None:
    if len(execution_order) != len(set(execution_order)):
        raise ValueError("Persisted plan execution_order contains duplicate nodes.")
    if set(execution_order) != set(nodes_by_id):
        raise ValueError(
            "Persisted plan execution_order must contain every node exactly once."
        )


def validate_render_plan_ownership(
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
