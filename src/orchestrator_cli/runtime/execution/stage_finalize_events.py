from __future__ import annotations

from orchestrator_cli.architecture.ports.artifacts import StageFinalizeResult

from .execution_activity import ExecutionTelemetry
from .execution_events import RuntimeEventContext, emit_runtime_log


def emit_stage_finalize_logs(
    telemetry: ExecutionTelemetry | None,
    result: StageFinalizeResult,
) -> None:
    empty_output_warnings = {
        f"Skipping empty output file {skipped_output.name}"
        for skipped_output in result.skipped_empty_outputs
    }
    if result.skipped_empty_outputs:
        for skipped_output in result.skipped_empty_outputs:
            emit_runtime_log(
                telemetry,
                "warning",
                f"Skipping empty output file {skipped_output.name}",
                "empty_output_skipped",
                context=RuntimeEventContext(
                    node_id=result.stage_name,
                    output_file=skipped_output,
                ),
                attributes={"result_file": str(result.result_file)},
            )
    for warning in result.warnings:
        if warning in empty_output_warnings:
            continue
        emit_runtime_log(
            telemetry,
            "warning",
            warning,
            "stage_finalize_warning",
            context=RuntimeEventContext(
                node_id=result.stage_name,
                output_file=result.result_file,
            ),
        )
    emit_runtime_log(
        telemetry,
        "info",
        (
            f"Finalized stage '{result.stage_name}' with "
            f"{len(result.included_outputs)} included outputs"
        ),
        "stage_finalized",
        context=RuntimeEventContext(
            node_id=result.stage_name,
            output_file=result.result_file,
        ),
        attributes={
            "included_output_count": len(result.included_outputs),
            "findings_file": (
                str(result.findings_file) if result.findings_file is not None else None
            ),
            "skipped_empty_output_count": len(result.skipped_empty_outputs),
        },
    )
