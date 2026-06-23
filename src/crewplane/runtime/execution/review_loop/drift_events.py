from __future__ import annotations

from pathlib import Path

from crewplane.architecture.ports import ArtifactStorePort
from crewplane.core.preflight.models import ProviderRecord

from ..common import ExecutionTelemetry, RuntimeEventContext, emit_runtime_log
from .types import DriftCheckResult


def path_relative_to_state_root(
    output: ArtifactStorePort,
    path: Path,
) -> str:
    state_root = output.stages_dir.parent.parent
    try:
        return str(path.relative_to(state_root))
    except ValueError:
        return str(path)


def format_drift_paths(
    output: ArtifactStorePort,
    paths: tuple[Path, ...],
) -> str:
    display_paths = [path_relative_to_state_root(output, path) for path in paths[:5]]
    if len(paths) > 5:
        display_paths.append(f"(+{len(paths) - 5} more)")
    return ", ".join(display_paths)


def emit_artifact_drift(
    telemetry: ExecutionTelemetry | None,
    output: ArtifactStorePort,
    node_id: str,
    task_id: str,
    provider: ProviderRecord,
    role_label: str,
    audit_round_num: int | None,
    round_num: int,
    drift: DriftCheckResult,
) -> None:
    if drift.warning_paths:
        emit_runtime_log(
            telemetry,
            level="warning",
            message=(
                f"Invocation for node '{node_id}' task '{task_id}' modified "
                f"unexpected artifacts: {format_drift_paths(output, drift.warning_paths)}"
            ),
            operation="review_loop_artifact_drift",
            context=RuntimeEventContext(
                node_id=node_id,
                provider=provider.provider,
                role=role_label,
                task_id=task_id,
                audit_round_num=audit_round_num,
                round_num=round_num,
            ),
            attributes={"unexpected_path_count": len(drift.warning_paths)},
        )
    if drift.fatal_paths:
        emit_runtime_log(
            telemetry,
            level="error",
            message=(
                f"Invocation for node '{node_id}' task '{task_id}' modified fatal "
                f"artifacts: {format_drift_paths(output, drift.fatal_paths)}"
            ),
            operation="review_loop_artifact_drift",
            context=RuntimeEventContext(
                node_id=node_id,
                provider=provider.provider,
                role=role_label,
                task_id=task_id,
                audit_round_num=audit_round_num,
                round_num=round_num,
            ),
            attributes={"fatal_path_count": len(drift.fatal_paths)},
        )
