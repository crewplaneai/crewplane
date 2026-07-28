from __future__ import annotations

from pathlib import Path

from crewplane.artifacts.generated_files.catalog import (
    generated_file_snapshot_rejection_summary,
)
from crewplane.runtime.workspace import PreparedWorkspace

from ..activity.events import InvocationMetadata
from .events import emit_artifact_capture_event
from .generated_file_changes import GeneratedFileChangeBaseline
from .generated_files import snapshot_invocation_generated_files_async
from .types import ProviderCallRequest


async def capture_invocation_generated_files(
    request: ProviderCallRequest,
    prepared_workspace: PreparedWorkspace,
    baseline: GeneratedFileChangeBaseline | None,
    metadata: InvocationMetadata,
) -> Path | None:
    try:
        workspace = await snapshot_invocation_generated_files_async(
            request,
            prepared_workspace,
            baseline,
        )
    except Exception as exc:
        request.runtime_context.generated_file_workspaces.record_capture_failure(
            request.node_id,
            request.output_file,
        )
        emit_artifact_capture_event(
            request.telemetry,
            metadata,
            "artifact_capture_failed",
            f"Provider output succeeded, but generated-file capture failed: {exc}",
            {"output_file": request.output_file.as_posix()},
        )
        return None

    rejection_summary = (
        generated_file_snapshot_rejection_summary(workspace)
        if workspace is not None
        else None
    )
    if rejection_summary is not None and rejection_summary.total_count:
        emit_artifact_capture_event(
            request.telemetry,
            metadata,
            "artifact_capture_partial",
            "Provider output succeeded with a partial generated-file capture.",
            {
                "output_file": request.output_file.as_posix(),
                "rejected_file_count": rejection_summary.total_count,
                "rejected_file_details_recorded": len(rejection_summary.recorded_files),
                "rejected_files_truncated": rejection_summary.truncated,
            },
        )
    return workspace
