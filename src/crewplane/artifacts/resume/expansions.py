from __future__ import annotations

from typing import TYPE_CHECKING

from crewplane.architecture.ports.artifacts import ArtifactStorePort
from crewplane.core.preflight.foreach.expansion import expansion_directory
from crewplane.core.preflight.foreach.models import ExpansionBatch
from crewplane.core.preflight.foreach.reconstruction import apply_expansion
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.core.preflight.serialization import canonical_json_bytes

from ..expansions import load_expansion, verify_caller_artifacts

if TYPE_CHECKING:
    from .validation import ValidatedResumeFrontier


def hydrate_expansions(
    frontier: ValidatedResumeFrontier,
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
) -> PreflightExecutionPlan:
    for group_id, selected in frontier.expansion_batches.items():
        template = next(
            template
            for template in plan.foreach_templates
            if template.group_id == group_id
        )
        batch, items = load_expansion(plan, template, frontier.source.run_dir)
        if batch != selected:
            raise ValueError(
                f"Selected expansion '{group_id}' changed before hydration"
            )
        verify_caller_artifacts(
            plan, batch, frontier.source.run_dir, frontier.source.results_dir
        )
        output.write_preflight_static_file(
            batch.state.collection.relative_path.removeprefix("preflight/"),
            canonical_json_bytes(items),
        )
        for evidence, item in zip(batch.state.items, items, strict=True):
            output.write_preflight_static_file(
                evidence.relative_path.removeprefix("preflight/"),
                canonical_json_bytes(item),
            )
        output.write_preflight_json(
            f"{expansion_directory(group_id).removeprefix('preflight/')}/expansion.json",
            batch,
        )
        plan = apply_expansion(plan, batch)
    return plan


def eligible_expansion_plan(
    base: PreflightExecutionPlan,
    batches: dict[str, ExpansionBatch],
    eligible_groups: set[str],
) -> PreflightExecutionPlan:
    for group_id, batch in batches.items():
        if group_id in eligible_groups:
            base = apply_expansion(base, batch)
    return base
