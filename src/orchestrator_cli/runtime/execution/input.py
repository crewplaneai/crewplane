from __future__ import annotations

from pathlib import Path

from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.core.preflight.models import PreflightExecutionNode

from .common import (
    CompiledRuntimeContext,
    ExecutionTelemetry,
    RuntimeEventContext,
    emit_runtime_log,
)


def execute_input_stage(
    stage: PreflightExecutionNode,
    output: ArtifactStorePort,
    runtime_context: CompiledRuntimeContext,
    telemetry: ExecutionTelemetry | None = None,
) -> None:
    node_dir = output.create_stage_dir(stage.id)
    if stage.input_content_ref is None:
        raise ValueError(f"Input node '{stage.id}' is missing source content.")

    resolved_source = _read_input_content(
        runtime_context.plan.context_root,
        stage.input_content_ref,
    )
    if not resolved_source.strip():
        raise RuntimeError(
            f"Resolved input content for node '{stage.id}' is empty after preflight assembly."
        )

    output_file = _input_output_file(node_dir)
    output_file.write_text(resolved_source, encoding="utf-8")
    emit_runtime_log(
        telemetry,
        level="info",
        message="Materialized raw input artifact.",
        operation="input_materialized",
        context=RuntimeEventContext(
            node_id=stage.id,
            output_file=output_file,
        ),
    )


def _input_output_file(node_dir: Path) -> Path:
    return node_dir / "input_round1.md"


def _read_input_content(context_root: str, content_ref: str) -> str:
    normalized_ref = Path(content_ref)
    if normalized_ref.is_absolute() or ".." in normalized_ref.parts:
        raise ValueError(f"Invalid input content reference '{content_ref}'.")
    source_path = Path(context_root) / "preflight" / normalized_ref
    return source_path.read_text(encoding="utf-8")
