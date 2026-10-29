"""Validate persisted preflight artifact locator contracts."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from crewplane.core.workflow.keywords import RESERVED_RUN_ROOT_NAMES

from . import plan_contract_records

if TYPE_CHECKING:
    from .compiled_records import PreflightExecutionNode

RESERVED_STAGE_ROOTS = frozenset((*RESERVED_RUN_ROOT_NAMES, "preflight"))


def validate_artifact_contract_uniqueness(
    nodes: Sequence[PreflightExecutionNode],
) -> None:
    occupied: list[tuple[str, tuple[str, ...], str, str]] = []
    for node in nodes:
        contract = node.artifact_contract
        if contract.stage_path is None:
            raise ValueError(
                f"Persisted node '{plan_contract_records.node_id(node)}' is missing "
                "its stage locator."
            )
        stage_parts = contract.stage_path.split("/")
        if stage_parts[0].casefold() in RESERVED_STAGE_ROOTS:
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
            occupied.append((root, PurePosixPath(path.casefold()).parts, path, node.id))

    occupied.sort()
    for previous, current in zip(occupied, occupied[1:], strict=False):
        root, parts, path, node_id = current
        previous_root, previous_parts, previous_path, previous_node = previous
        if root == previous_root and parts[: len(previous_parts)] == previous_parts:
            raise ValueError(
                f"Persisted nodes '{previous_node}' and '{node_id}' have overlapping "
                f"{root} artifact locators: '{previous_path}' and '{path}'."
            )
