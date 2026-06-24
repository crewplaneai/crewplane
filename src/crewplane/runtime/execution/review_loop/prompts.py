from __future__ import annotations

from crewplane.core.preflight.models import PreflightExecutionNode
from crewplane.core.review_contract import REVIEW_RESPONSE_INSTRUCTION

from ..common import (
    ExecutionTelemetry,
    PromptBudgetExceededError,
    RuntimeEventContext,
    compiled_token_budget,
    emit_runtime_log,
)
from .types import ExecutorRoundArtifact

REVIEWER_ONLY_INSTRUCTION = (
    "You are acting only as a reviewer.\n"
    "Review only the current executor output(s) shown below.\n"
    "Do not modify files, apply fixes, run changes, or change the workspace.\n"
    "Approve the cycle only when all major and minor issues are resolved.\n"
    "Use NITS_ONLY only when only optional nitpicks remain.\n"
    "Use NO_FINDINGS only when no issues remain at all."
)
INITIAL_REVIEWER_ONLY_INSTRUCTION = (
    "You are acting only as a reviewer.\n"
    "Review only the existing context shown below before any same-node executor "
    "candidate exists.\n"
    "Do not modify files, apply fixes, run changes, or change the workspace.\n"
    "Approve the initial pass only when the existing context has no major or "
    "minor issues.\n"
    "Use NITS_ONLY only when only optional nitpicks remain.\n"
    "Use NO_FINDINGS only when no issues remain at all."
)
INITIAL_REVIEW_APPROVED_HANDOFF = (
    "The initial reviewer pass found no unresolved major or minor issues in the "
    "reviewed context. Preserve that reviewed state, avoid broad rewrites, and "
    "make only the minimal necessary changes required to produce this node's "
    "canonical executor candidate."
)
INITIAL_REVIEW_FAILURE_HANDOFF = (
    "The initial reviewer pass could not approve the reviewed context because at "
    "least one reviewer invocation failed. Do not invent reviewer feedback. "
    "Preserve the reviewed context where possible and make only the minimal "
    "necessary changes required to produce this node's canonical executor "
    "candidate."
)
INITIAL_REVIEW_BLOCKED_HANDOFF = (
    "The initial reviewer pass did not produce approval or actionable normalized "
    "feedback. Treat the reviewed context as blocked, preserve it where possible, "
    "and make only the minimal necessary changes required to produce this node's "
    "canonical executor candidate."
)
INITIAL_REVIEW_TASK_CONTEXT = (
    "Review the existing context before the local round 1 executor writes this "
    "node's canonical candidate."
)
REVIEWER_REMEDIATION_INSTRUCTION = (
    "When previous unresolved review state is provided, verify whether those major "
    "and minor issues are now resolved. Report new major or minor issues only when "
    "the current candidate introduces or reveals them."
)
EXECUTOR_CANONICAL_OUTPUT_INSTRUCTION = (
    "Return the full revised candidate in this response.\n"
    "This response becomes the canonical artifact for this round.\n"
    "Do not rely on editing prior .crewplane artifacts or redirecting review to "
    "another round file.\n"
    "Start directly with the candidate document title or first candidate section.\n"
    "Do not include progress notes, process commentary, or references to what you "
    "inspected before the candidate."
)


def build_executor_prompt(
    base_prompt: str,
    previous_candidate_context: str | None,
    previous_review_packet: str | None,
    initial_review_handoff: str | None = None,
) -> str:
    sections = [base_prompt, EXECUTOR_CANONICAL_OUTPUT_INSTRUCTION]
    if initial_review_handoff:
        sections.append(f"Initial reviewer handoff:\n{initial_review_handoff}")
    if previous_review_packet:
        if previous_candidate_context:
            sections.append(
                f"Previous canonical candidate:\n{previous_candidate_context}"
            )
        sections.extend(
            [
                f"Previous unresolved review state:\n{previous_review_packet}",
                (
                    "Address all remaining major and minor issues in your update. "
                    "Nitpicks are optional unless they hide a correctness problem."
                ),
            ]
        )
    return "\n\n".join(sections)


def resolve_previous_candidate_context(
    node: PreflightExecutionNode,
    previous_executor_outputs: list[ExecutorRoundArtifact] | None,
    telemetry: ExecutionTelemetry | None,
) -> str | None:
    if not previous_executor_outputs:
        return None
    context = build_review_context(previous_executor_outputs)
    _check_previous_candidate_budget(node, context, telemetry)
    return context


def _check_previous_candidate_budget(
    node: PreflightExecutionNode,
    context: str,
    telemetry: ExecutionTelemetry | None,
) -> None:
    budget = compiled_token_budget(node)
    fail_threshold = budget.get("fail_threshold_chars")
    warn_threshold = budget.get("warn_threshold_chars")
    if fail_threshold is None and warn_threshold is None:
        return

    char_count = len(context)
    if fail_threshold is not None and char_count > fail_threshold:
        raise PromptBudgetExceededError(
            "Prompt budget exceeded for node "
            f"'{node.id}': previous canonical candidate resolves to "
            f"{char_count} chars, exceeding fail threshold {fail_threshold}. "
            "Shorten the prior candidate or raise "
            "the threshold intentionally."
        )
    if warn_threshold is not None and char_count > warn_threshold:
        emit_runtime_log(
            telemetry,
            "warning",
            (
                "Prompt budget warning for node "
                f"'{node.id}': previous canonical candidate resolves to "
                f"{char_count} chars, exceeding warn threshold "
                f"{warn_threshold}. Shorten the prior candidate or "
                "raise the threshold intentionally."
            ),
            "prompt_budget_warning",
            context=RuntimeEventContext(node_id=node.id),
            attributes={
                "upstream_node_id": node.id,
                "upstream_artifact_name": "previous_canonical_candidate",
                "char_count": char_count,
                "warn_threshold_chars": warn_threshold,
            },
        )


def build_review_context(
    executor_outputs: list[ExecutorRoundArtifact],
) -> str:
    return "\n\n".join(
        (
            f"=== {artifact.provider.provider} executor output ===\n"
            f"Artifact: {artifact.output_file}\n\n{artifact.content}"
        )
        for artifact in executor_outputs
    )


def build_reviewer_prompt(
    base_prompt: str,
    review_context: str,
    previous_review_packet: str | None,
    review_context_heading: str = "Current executor output(s)",
    review_context_note: str | None = None,
    reviewer_instruction: str = REVIEWER_ONLY_INSTRUCTION,
) -> str:
    sections = [
        reviewer_instruction,
        f"Task context:\n{base_prompt}",
    ]
    if previous_review_packet:
        sections.append(REVIEWER_REMEDIATION_INSTRUCTION)
        sections.append(f"Previous unresolved review state:\n{previous_review_packet}")
    sections.extend(
        [
            _review_context_section(
                review_context_heading,
                review_context_note,
                review_context,
            ),
            REVIEW_RESPONSE_INSTRUCTION,
        ]
    )
    return "\n\n".join(sections)


def _review_context_section(
    heading: str,
    note: str | None,
    review_context: str,
) -> str:
    if note:
        return f"{heading}:\n{note}\n\n{review_context}"
    return f"{heading}:\n\n{review_context}"
