from __future__ import annotations

from pathlib import Path

from crewplane.architecture.contracts import LogLevel, NodeArtifactRequest
from crewplane.architecture.ports import ArtifactStorePort
from crewplane.artifacts.atomic import atomic_write_text
from crewplane.artifacts.expansions import read_expansion_artifact
from crewplane.core.preflight.compiled_records import PreflightExecutionNode

from .common import (
    CompiledRuntimeContext,
    ExecutionTelemetry,
    RuntimeEventContext,
    emit_runtime_log,
)
from .errors import NodeExecutionError
from .preflight_files import read_verified_static_text
from .workspace_files import resolve_project_initial_workspace_file


class InputContentError(NodeExecutionError):
    """Raised when an input node cannot materialize usable content."""


def execute_input_stage(
    stage: PreflightExecutionNode,
    output: ArtifactStorePort,
    runtime_context: CompiledRuntimeContext,
    telemetry: ExecutionTelemetry | None = None,
) -> None:
    node_dir = output.create_node_dir(
        NodeArtifactRequest(stage.id, stage.artifact_contract)
    )
    expansion = stage.expansion
    item = (
        runtime_context.plan.expansions[expansion.group_id].items[expansion.ordinal]
        if expansion is not None and expansion.ordinal is not None
        else None
    )
    if item is not None and stage.input_content_ref == item.relative_path:
        try:
            input_content = read_expansion_artifact(output.stages_dir, item).decode(
                "utf-8"
            )
        except (OSError, ValueError) as exc:
            raise InputContentError(f"Input node '{stage.id}': {exc}") from exc
    elif stage.input_workspace_file_locator_id is not None:
        input_content = _read_workspace_input_content(
            runtime_context,
            stage.input_workspace_file_locator_id,
        )
    elif stage.input_content_ref is not None:
        try:
            input_content = read_verified_static_text(
                runtime_context.plan, stage.input_content_ref
            )
        except ValueError as exc:
            raise InputContentError(f"Input node '{stage.id}': {exc}") from exc

    else:
        raise ValueError(f"Input node '{stage.id}' is missing source content.")

    if not input_content.strip():
        raise InputContentError(
            f"Resolved input content for node '{stage.id}' is empty after preflight assembly."
        )

    output_file = _input_output_file(node_dir)
    atomic_write_text(output_file, input_content)
    emit_runtime_log(
        telemetry,
        level=LogLevel.INFO,
        message="Materialized raw input artifact.",
        operation="input_materialized",
        context=RuntimeEventContext(
            node_id=stage.id,
            output_file=output_file,
        ),
    )


def _input_output_file(node_dir: Path) -> Path:
    return node_dir / "input_round1.md"


def _read_workspace_input_content(
    runtime_context: CompiledRuntimeContext,
    locator_id: str,
) -> str:
    return resolve_project_initial_workspace_file(
        runtime_context.plan,
        locator_id,
    ).text
