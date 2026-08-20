"""Validate persisted preflight artifact locator contracts."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from crewplane.core.workflow.keywords import RESERVED_RUN_ROOT_NAMES

from . import plan_contract_records

if TYPE_CHECKING:
    from .models import PreflightExecutionNode

RESERVED_STAGE_ROOTS = frozenset((*RESERVED_RUN_ROOT_NAMES, "preflight"))


def validate_artifact_contract_uniqueness(
    nodes: Sequence[PreflightExecutionNode],
) -> None:
    occupied: dict[tuple[str, str], str] = {}
    for node in nodes:
        contract = node.artifact_contract
        if contract.stage_path is None:
            raise ValueError(
                f"Persisted node '{plan_contract_records.node_id(node)}' is missing "
                "its stage locator."
            )
        stage_parts = contract.stage_path.split("/")
        if stage_parts[0] in RESERVED_STAGE_ROOTS:
            raise ValueError(
                f"Persisted node '{plan_contract_records.node_id(node)}' uses "
                f"reserved stage root '{stage_parts[0]}'."
            )
        if contract.result_path != contract.output_path:
            raise ValueError(
                f"Persisted node '{plan_contract_records.node_id(node)}' has "
                "conflicting output locators."
            )
        if bool(node.findings) != (contract.findings_path is not None):
            raise ValueError(
                f"Persisted node '{plan_contract_records.node_id(node)}' has an "
                "inconsistent findings locator."
            )
        expected_log_path = f"{contract.stage_path}/logs"
        if contract.log_path != expected_log_path:
            raise ValueError(
                f"Persisted node '{plan_contract_records.node_id(node)}' has a log "
                "locator outside its stage."
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
                    f"Persisted nodes '{existing[1]}' and "
                    f"'{plan_contract_records.node_id(node)}' have overlapping "
                    f"{root} artifact locators: '{existing[0][1]}' and '{path}'."
                )
            occupied[(root, path)] = plan_contract_records.node_id(node)


def _paths_overlap(left: str, right: str) -> bool:
    left_path = Path(left)
    right_path = Path(right)
    return (
        left_path == right_path
        or left_path.is_relative_to(right_path)
        or right_path.is_relative_to(left_path)
    )
