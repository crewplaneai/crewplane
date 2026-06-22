from __future__ import annotations

from dataclasses import dataclass

from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.core.preflight.models import PreflightExecutionNode

from .activity.events import RuntimeEventContext, emit_runtime_log
from .activity.telemetry import ExecutionTelemetry
from .fragment_assembler import (
    ResolvedPrompt,
    assemble_prompt_details,
    inspect_runtime_locators,
)
from .runtime_context import CompiledRuntimeContext
from .workspace_files import ResolvedWorkspaceFile, WorkspaceCandidateSourceContext


class PromptBudgetExceededError(RuntimeError):
    """Raised when a prompt exceeds the configured node artifact budget."""


@dataclass(frozen=True)
class PromptBudgetThresholds:
    fail_threshold_chars: int | None
    warn_threshold_chars: int | None


@dataclass(frozen=True)
class PromptBudgetInspection:
    display_name: str
    char_count: int
    shorten_advice: str
    warning_attributes: dict[str, object]


def compiled_token_budget(node: PreflightExecutionNode) -> dict[str, int | None]:
    budget = node.execution_policy.token_budget
    if budget is None:
        return {
            "fail_threshold_chars": None,
            "warn_threshold_chars": None,
        }
    return {
        "fail_threshold_chars": budget.fail_threshold_chars,
        "warn_threshold_chars": budget.warn_threshold_chars,
    }


def resolve_prompt_with_output_budget(
    runtime_context: CompiledRuntimeContext,
    node: PreflightExecutionNode,
    output: ArtifactStorePort,
    role: str,
    telemetry: ExecutionTelemetry | None,
    workspace_candidate_source: bool = False,
    workspace_candidate_context: WorkspaceCandidateSourceContext | None = None,
) -> str:
    return resolve_prompt_with_output_budget_details(
        runtime_context,
        node,
        output,
        role,
        telemetry,
        workspace_candidate_source=workspace_candidate_source,
        workspace_candidate_context=workspace_candidate_context,
    ).text


def resolve_prompt_with_output_budget_details(
    runtime_context: CompiledRuntimeContext,
    node: PreflightExecutionNode,
    output: ArtifactStorePort,
    role: str,
    telemetry: ExecutionTelemetry | None,
    workspace_candidate_source: bool = False,
    workspace_candidate_context: WorkspaceCandidateSourceContext | None = None,
) -> ResolvedPrompt:
    budget_payload = compiled_token_budget(node)
    thresholds = PromptBudgetThresholds(
        fail_threshold_chars=budget_payload.get("fail_threshold_chars"),
        warn_threshold_chars=budget_payload.get("warn_threshold_chars"),
    )
    inspections = inspect_runtime_locators(
        runtime_context.plan,
        node,
        role,
        output,
    )
    for inspection in inspections:
        _enforce_prompt_budget(
            node,
            PromptBudgetInspection(
                display_name=f"'{inspection.node_id}.{inspection.artifact_name}'",
                char_count=inspection.char_count,
                shorten_advice=(
                    "Shorten the upstream artifact or raise the threshold "
                    "intentionally."
                ),
                warning_attributes={
                    "upstream_node_id": inspection.node_id,
                    "upstream_artifact_name": inspection.artifact_name,
                },
            ),
            thresholds,
            telemetry,
        )
    resolved_prompt = assemble_prompt_details(
        runtime_context.plan,
        node,
        role,
        output,
        runtime_context.secret_context,
        workspace_candidate_source=workspace_candidate_source,
        workspace_candidate_context=workspace_candidate_context,
    )
    for inspection in _workspace_file_budget_inspections(
        resolved_prompt.workspace_files
    ):
        _enforce_prompt_budget(node, inspection, thresholds, telemetry)
    if not resolved_prompt.text.strip():
        raise RuntimeError(
            f"Resolved {role} prompt for node '{node.id}' is empty after fragment assembly."
        )
    return resolved_prompt


def _workspace_file_budget_inspections(
    workspace_files: tuple[ResolvedWorkspaceFile, ...],
) -> tuple[PromptBudgetInspection, ...]:
    inspections: list[PromptBudgetInspection] = []
    seen_locator_ids: set[str] = set()
    for workspace_file in workspace_files:
        locator = workspace_file.locator
        if locator.locator_id in seen_locator_ids:
            continue
        seen_locator_ids.add(locator.locator_id)
        inspections.append(
            PromptBudgetInspection(
                display_name=f"workspace file '{locator.workspace_relative_path}'",
                char_count=len(workspace_file.text),
                shorten_advice=(
                    "Shorten the workspace file or raise the threshold intentionally."
                ),
                warning_attributes={
                    "upstream_node_id": locator.node_id,
                    "upstream_artifact_name": f"file:{locator.workspace_relative_path}",
                    "workspace_file_locator_id": locator.locator_id,
                    "workspace_relative_path": locator.workspace_relative_path,
                    "source_class": locator.source_class,
                    "byte_size": workspace_file.byte_size,
                },
            )
        )
    return tuple(inspections)


def _enforce_prompt_budget(
    node: PreflightExecutionNode,
    inspection: PromptBudgetInspection,
    thresholds: PromptBudgetThresholds,
    telemetry: ExecutionTelemetry | None,
) -> None:
    fail_threshold = thresholds.fail_threshold_chars
    if fail_threshold is not None and inspection.char_count > fail_threshold:
        raise PromptBudgetExceededError(
            "Prompt budget exceeded for node "
            f"'{node.id}': {inspection.display_name} resolves to "
            f"{inspection.char_count} chars, exceeding fail threshold "
            f"{fail_threshold}. {inspection.shorten_advice}"
        )
    warn_threshold = thresholds.warn_threshold_chars
    if warn_threshold is None or inspection.char_count <= warn_threshold:
        return
    emit_runtime_log(
        telemetry,
        "warning",
        (
            "Prompt budget warning for node "
            f"'{node.id}': {inspection.display_name} resolves "
            f"to {inspection.char_count} chars, exceeding warn threshold "
            f"{warn_threshold}. {inspection.shorten_advice}"
        ),
        "prompt_budget_warning",
        context=RuntimeEventContext(node_id=node.id),
        attributes={
            **inspection.warning_attributes,
            "char_count": inspection.char_count,
            "warn_threshold_chars": warn_threshold,
        },
    )
