from __future__ import annotations

from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.core.preflight.models import PreflightExecutionNode

from .execution_activity import ExecutionTelemetry
from .execution_events import RuntimeEventContext, emit_runtime_log
from .fragment_assembler import assemble_prompt, inspect_runtime_locators
from .runtime_context import CompiledRuntimeContext


class PromptBudgetExceededError(RuntimeError):
    """Raised when a prompt exceeds the configured node artifact budget."""


def compiled_token_budget(node: PreflightExecutionNode) -> dict[str, int | None]:
    budget = node.execution_policy.token_budget or {}
    return {
        "fail_threshold_chars": budget.get("fail_threshold_chars"),
        "warn_threshold_chars": budget.get("warn_threshold_chars"),
    }


def resolve_prompt_with_output_budget(
    runtime_context: CompiledRuntimeContext,
    node: PreflightExecutionNode,
    output: ArtifactStorePort,
    role: str,
    telemetry: ExecutionTelemetry | None,
) -> str:
    budget_payload = compiled_token_budget(node)
    warn_threshold = budget_payload.get("warn_threshold_chars")
    fail_threshold = budget_payload.get("fail_threshold_chars")
    inspections = inspect_runtime_locators(
        runtime_context.plan,
        node,
        role,
        output,
    )
    for inspection in inspections:
        artifact_template = f"'{inspection.node_id}.{inspection.artifact_name}'"
        if fail_threshold is not None and inspection.char_count > fail_threshold:
            raise PromptBudgetExceededError(
                "Prompt budget exceeded for node "
                f"'{node.id}': {artifact_template} resolves to "
                f"{inspection.char_count} chars, exceeding fail threshold "
                f"{fail_threshold}. Shorten the upstream artifact or "
                "raise the threshold intentionally."
            )
        if warn_threshold is not None and inspection.char_count > warn_threshold:
            emit_runtime_log(
                telemetry,
                "warning",
                (
                    "Prompt budget warning for node "
                    f"'{node.id}': {artifact_template} resolves "
                    f"to {inspection.char_count} chars, exceeding warn threshold "
                    f"{warn_threshold}. Shorten the upstream artifact "
                    "or raise the threshold intentionally."
                ),
                "prompt_budget_warning",
                context=RuntimeEventContext(node_id=node.id),
                attributes={
                    "upstream_node_id": inspection.node_id,
                    "upstream_artifact_name": inspection.artifact_name,
                    "char_count": inspection.char_count,
                    "warn_threshold_chars": warn_threshold,
                },
            )
    resolved_prompt = assemble_prompt(
        runtime_context.plan,
        node,
        role,
        output,
        runtime_context.secret_context,
    )
    if not resolved_prompt.strip():
        raise RuntimeError(
            f"Resolved {role} prompt for node '{node.id}' is empty after fragment assembly."
        )
    return resolved_prompt
