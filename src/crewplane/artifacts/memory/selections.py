"""Publishable frozen recall receipts and protected historical agreement checks."""

from __future__ import annotations

import json
from pathlib import Path

from crewplane.architecture.safe_file_reads import read_contained_bytes
from crewplane.core.memory.retrieval import MemorySelection
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.version import SCHEMA_VERSION


def frozen_memory_selections(plan: PreflightExecutionPlan) -> list[MemorySelection]:
    generated = {node.id for node in plan.nodes if node.expansion is not None}
    return [
        *(
            selection
            for selection in plan.memory_selections
            if selection.node_id not in generated
        ),
        *(
            selection
            for template in plan.foreach_templates
            for selection in template.memory_selections
        ),
    ]


def memory_selection_receipt(plan: PreflightExecutionPlan) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "workflow_signature": plan.workflow_signature,
        "selections": [
            selection.model_dump(mode="json")
            for selection in frozen_memory_selections(plan)
        ],
    }


def validate_memory_selection_receipt(
    plan: PreflightExecutionPlan, run_dir: Path
) -> None:
    if not frozen_memory_selections(plan):
        return
    receipt = json.loads(
        read_contained_bytes(run_dir, "preflight/memory-selection.json")
    )
    if receipt != memory_selection_receipt(plan):
        raise ValueError(
            "Memory selection receipt disagrees with its frozen execution plan."
        )


def validate_historical_memory_selection(
    plan: PreflightExecutionPlan, run_dir: Path
) -> None:
    selections = frozen_memory_selections(plan)
    if not selections:
        return
    historical = PreflightExecutionPlan.model_validate_json(
        read_contained_bytes(run_dir, "preflight/execution-plan.json")
    )
    validate_memory_selection_receipt(historical, run_dir)
    if historical.workflow_signature != plan.workflow_signature or [
        selection.semantic_payload()
        for selection in frozen_memory_selections(historical)
    ] != [selection.semantic_payload() for selection in selections]:
        raise ValueError(
            "Historical memory selections do not match the current execution identity."
        )
