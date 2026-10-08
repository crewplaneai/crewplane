"""Prepare initial review handoffs and canonical candidates for fresh audits."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict
from functools import partial
from pathlib import Path

from crewplane.architecture.contracts.artifacts import build_task_round_filename
from crewplane.architecture.ports import ArtifactStorePort
from crewplane.artifacts.atomic import atomic_write_json
from crewplane.core.preflight.models import PreflightExecutionNode
from crewplane.core.review_checkpoint_state import CheckpointPhase
from crewplane.core.workflow.keywords import ProviderRole

from ..common import (
    CompiledRuntimeContext,
    ExecutionTelemetry,
    resolve_prompt_with_output_budget_details,
)
from ..fragment_assembler import (
    ResolvedPrompt,
    stream_has_runtime_dynamic_workspace_locator,
)
from ..provider_call import publish_invocation_output, read_bound_invocation_output
from ..reviews.consensus import check_consensus
from ..workspace_files import ResolvedWorkspaceFile
from ..workspace_files.source_resolution import WorkspaceCandidateSourceContext
from .audit_io import complete_audit_io
from .prompts import (
    INITIAL_REVIEW_APPROVED_HANDOFF,
    INITIAL_REVIEW_BLOCKED_HANDOFF,
    INITIAL_REVIEW_FAILURE_HANDOFF,
    INITIAL_REVIEW_TASK_CONTEXT,
    INITIAL_REVIEWER_ONLY_INSTRUCTION,
)
from .rounds import run_executor_round, run_reviewer_round
from .state import render_unresolved_review_packet
from .types import (
    ExecutorRoundArtifact,
    ExecutorRoundRequest,
    ReviewerRoundRequest,
    ReviewerRoundRunResult,
    ReviewLoopProgress,
    ReviewLoopRunContext,
)


def resolve_reviewer_prompt_context(
    runtime_context: CompiledRuntimeContext,
    stage: PreflightExecutionNode,
    output: ArtifactStorePort,
    telemetry: ExecutionTelemetry | None,
) -> ResolvedPrompt:
    """Resolve static reviewer context, deferring candidate workspace locators.

    Return an empty prompt when the stream needs a runtime candidate; each review
    then resolves it against its own audit and round. Otherwise preserve budget
    checks and workspace-file evidence, propagating resolution errors.
    """
    if stream_has_runtime_dynamic_workspace_locator(
        runtime_context.plan,
        stage,
        ProviderRole.REVIEWER,
    ):
        return ResolvedPrompt("")
    return resolve_prompt_with_output_budget_details(
        runtime_context,
        stage,
        output,
        role=ProviderRole.REVIEWER,
        telemetry=telemetry,
    )


async def initial_audit_executor_outputs(
    context: ReviewLoopRunContext,
    progress: ReviewLoopProgress,
    audit_dir: Path,
    audit_context: int | None,
    initial_review_handoff: str | None = None,
) -> list[ExecutorRoundArtifact]:
    """Seed the latest valid outputs, or invoke the initial executor round.

    Retain progress and registry ownership until seeding I/O drains, including on
    cancellation. New executor runs update drift counters only after completion.
    """
    if progress.latest_executor_outputs is not None:
        return await complete_audit_io(
            partial(
                seed_executor_outputs,
                runtime_context=context.runtime_context,
                node_id=context.stage.id,
                artifact_dir=audit_dir,
                executor_outputs=progress.latest_executor_outputs,
                audit_round_num=audit_context,
                round_num=1,
            )
        )

    executor_run = await run_executor_round(
        ExecutorRoundRequest(
            runtime_context=context.runtime_context,
            node=context.stage,
            output=context.output,
            node_dir=context.node_dir,
            invoker=context.invoker,
            telemetry=context.telemetry,
            executors=context.executors,
            audit_round_num=audit_context,
            round_num=1,
            artifact_dir=audit_dir,
            executor_prompt=context.executor_prompt,
            executor_prompt_workspace_files=context.executor_prompt_workspace_files,
            previous_review_packet=None,
            previous_executor_outputs=None,
            initial_review_handoff=initial_review_handoff,
        )
    )
    progress.record_initial_executor_run(executor_run)
    return executor_run.outputs


async def initial_pre_review_handoff(
    context: ReviewLoopRunContext,
    progress: ReviewLoopProgress,
    audit_dir: Path,
    audit_context: int | None,
    audit_round_num: int,
    commit_transition: Callable[[CheckpointPhase, int], Awaitable[None]],
) -> str | None:
    """Reuse round-zero reviews or run reviewer-first review before the candidate.

    Record completed reviews before committing the executor transition and rendering
    their handoff. Later audits and executor-first runs return None. Invocation and
    checkpoint errors propagate without discarding recorded review evidence.
    """
    if audit_round_num == 1 and progress.initial_review_completed:
        return _initial_review_handoff_from_result(
            ReviewerRoundRunResult(
                progress.initial_reviews,
                0,
                len(progress.initial_failures),
                progress.initial_failures,
            )
        )
    if not _should_run_initial_pre_review(context, progress, audit_round_num):
        return None

    reviewer_prompt_context, reviewer_prompt_workspace_files = await complete_audit_io(
        partial(_initial_pre_review_prompt, context, audit_context)
    )
    reviewer_run = await run_reviewer_round(
        ReviewerRoundRequest(
            runtime_context=context.runtime_context,
            node=context.stage,
            output=context.output,
            node_dir=context.node_dir,
            invoker=context.invoker,
            telemetry=context.telemetry,
            reviewers=context.reviewers,
            audit_round_num=audit_context,
            round_num=0,
            artifact_dir=audit_dir,
            reviewer_prompt_context=INITIAL_REVIEW_TASK_CONTEXT,
            reviewer_prompt_workspace_files=reviewer_prompt_workspace_files,
            review_context=reviewer_prompt_context,
            previous_review_packet=None,
            review_context_heading="Existing review context",
            review_context_note=(
                "No same-node executor candidate exists yet. Review the existing "
                "context before the local round 1 executor writes a canonical "
                "candidate."
            ),
            reviewer_instruction=INITIAL_REVIEWER_ONLY_INSTRUCTION,
        )
    )
    progress.record_initial_reviewer_run(reviewer_run)
    await commit_transition("executors", 1)
    return _initial_review_handoff_from_result(reviewer_run)


def _should_run_initial_pre_review(
    context: ReviewLoopRunContext,
    progress: ReviewLoopProgress,
    audit_round_num: int,
) -> bool:
    return (
        context.stage.execution_policy.review_starts_with == "reviewer"
        and audit_round_num == 1
        and progress.latest_executor_outputs is None
    )


def _initial_pre_review_prompt(
    context: ReviewLoopRunContext,
    audit_context: int | None,
) -> tuple[str, tuple[ResolvedWorkspaceFile, ...]]:
    if context.reviewer_prompt_context:
        return (
            context.reviewer_prompt_context,
            context.reviewer_prompt_workspace_files,
        )
    resolved_prompt = resolve_prompt_with_output_budget_details(
        context.runtime_context,
        context.stage,
        context.output,
        role=ProviderRole.REVIEWER,
        telemetry=context.telemetry,
        workspace_candidate_context=WorkspaceCandidateSourceContext(
            role_label=ProviderRole.REVIEWER,
            round_num=0,
            audit_round_num=audit_context,
            phase="initial_pre_review",
        ),
    )
    return resolved_prompt.text, resolved_prompt.workspace_files


def _initial_review_handoff_from_result(
    reviewer_run: ReviewerRoundRunResult,
) -> str:
    if reviewer_run.reviewer_failure_count > 0:
        return INITIAL_REVIEW_FAILURE_HANDOFF

    unresolved_packet = render_unresolved_review_packet(reviewer_run.outputs)
    if unresolved_packet is not None:
        return unresolved_packet

    if check_consensus([artifact.evaluation for artifact in reviewer_run.outputs]):
        return INITIAL_REVIEW_APPROVED_HANDOFF

    return INITIAL_REVIEW_BLOCKED_HANDOFF


def seed_executor_outputs(
    runtime_context: CompiledRuntimeContext,
    node_id: str,
    artifact_dir: Path,
    executor_outputs: list[ExecutorRoundArtifact],
    audit_round_num: int | None,
    round_num: int,
) -> list[ExecutorRoundArtifact]:
    """Seed bound candidates in input order, preserving identity and provenance.

    Require source bytes to match the bound signature and rendered content to
    match the artifact. Publish verified bytes and recovery data before aliasing
    generated workspaces and writing the candidate identity sidecar. Errors
    propagate without rolling back earlier publications.
    The caller retains ownership of the publication registry and workspace mappings.
    """
    seeded_outputs: list[ExecutorRoundArtifact] = []
    for artifact in executor_outputs:
        output_file = artifact_dir / build_task_round_filename(
            artifact.task_id, round_num
        )
        if artifact.output_signature is None:
            raise RuntimeError(
                "Cannot seed an executor output without a bound runtime publication: "
                f"{artifact.output_file.as_posix()}"
            )
        if (
            read_bound_invocation_output(
                artifact.output_file, artifact.output_signature
            )
            != artifact.content
        ):
            raise RuntimeError(
                "Executor content does not match its bound output bytes: "
                f"{artifact.output_file.as_posix()}"
            )
        output_signature = publish_invocation_output(
            artifact.output_file,
            output_file,
            runtime_context.runtime_publications,
            artifact.output_signature,
        )
        runtime_context.generated_file_workspaces.alias_output_file(
            node_id,
            artifact.output_file,
            output_file,
        )
        if artifact.candidate_identity is not None:
            atomic_write_json(
                output_file.with_suffix(".candidate.json"),
                asdict(artifact.candidate_identity),
            )
        seeded_outputs.append(
            ExecutorRoundArtifact(
                provider=artifact.provider,
                task_id=artifact.task_id,
                content=artifact.content,
                output_file=output_file,
                audit_round_num=audit_round_num,
                round_num=round_num,
                output_signature=output_signature,
                candidate_identity=artifact.candidate_identity,
                producer_audit=artifact.producer_audit or artifact.audit_round_num or 1,
                producer_round=artifact.producer_round or artifact.round_num,
            )
        )
    return seeded_outputs
