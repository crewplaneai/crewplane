"""Prepare run history and hydrate validated artifacts for real execution."""

from __future__ import annotations

from crewplane.architecture.ports import ArtifactStorePort
from crewplane.artifacts.resume.checkpoint_hydration import hydrate_review_checkpoints
from crewplane.artifacts.resume.hydration import hydrate_resume_frontier
from crewplane.core.preflight import PreflightCompilationPreview, PreflightExecutionPlan

from .context import WorkflowRunContext
from .execution_helpers import fulfill_historical_branch_exports, handle_duplicate_skip
from .manifest import print_resume_context_message
from .resume import ResumePlan, build_resume_plan


def prepare_run_resume(
    context: WorkflowRunContext,
    preview: PreflightCompilationPreview,
    force: bool,
) -> ResumePlan | None:
    """Reconcile history before allocation; return None for a duplicate skip."""
    resume_plan = build_resume_plan(
        context.config,
        context.source,
        preview,
        context.project_root,
        context.state_dir,
        force,
    )
    if handle_duplicate_skip(context, preview, resume_plan):
        return None
    resume_source = resume_plan.decision.resume_source
    if resume_source is not None:
        fulfill_historical_branch_exports(
            context, preview, resume_source, resume_plan.resumed_node_ids
        )
    return resume_plan


def hydrate_run_resume(
    context: WorkflowRunContext,
    resume_plan: ResumePlan,
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
) -> None:
    """Restore results and checkpoints after expansion, before component setup."""
    frontier = resume_plan.frontier
    if frontier is None:
        return
    hydrate_resume_frontier(frontier, plan, output)
    hydrate_review_checkpoints(frontier, plan, output)
    resume_source = resume_plan.decision.resume_source
    if resume_source is not None:
        print_resume_context_message(
            context, len(resume_plan.resumed_node_ids), resume_source.manifest.run_id
        )
