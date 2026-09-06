from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .snapshot import (
    SnapshotDriftSummary,
    WorkspaceSnapshotLimitError,
    WorkspaceSnapshotPolicy,
    snapshot_drift_summary,
    snapshot_entries,
)
from .terminalization import workspace_diagnostic


@dataclass(frozen=True)
class SnapshotSuccessOutcome:
    diagnostics: tuple[dict[str, str], ...]
    result: dict[str, object]


def snapshot_success_outcome(
    checkout_root: Path,
    initial_entries: dict[str, str],
    policy: WorkspaceSnapshotPolicy,
) -> SnapshotSuccessOutcome:
    try:
        current_entries = snapshot_entries(checkout_root, policy)
    except WorkspaceSnapshotLimitError as exc:
        return _snapshot_limit_outcome(exc)
    summary = snapshot_drift_summary(initial_entries, current_entries)
    return _snapshot_drift_outcome(summary)


def _snapshot_limit_outcome(
    error: WorkspaceSnapshotLimitError,
) -> SnapshotSuccessOutcome:
    return SnapshotSuccessOutcome(
        diagnostics=(
            workspace_diagnostic(
                "warning",
                "Snapshot checkout changes were discarded; final drift is "
                f"unknown because reporting reached a limit: {error}",
            ),
        ),
        result={
            "lineage_produced": False,
            "drift_scan_complete": False,
            "drift_scan_limit_reason": str(error),
        },
    )


def _snapshot_drift_outcome(summary: SnapshotDriftSummary) -> SnapshotSuccessOutcome:
    diagnostics: tuple[dict[str, str], ...] = ()
    if summary.changed_path_count:
        diagnostics = (
            workspace_diagnostic(
                "warning",
                "Snapshot checkout changes were discarded "
                f"({summary.changed_path_count} path(s)).",
            ),
        )
    return SnapshotSuccessOutcome(
        diagnostics=diagnostics,
        result={
            "lineage_produced": False,
            "drift_scan_complete": True,
            "snapshot_drift_discarded": bool(summary.changed_path_count),
            "changed_path_count": summary.changed_path_count,
            "changed_paths": list(summary.changed_paths),
            "changed_paths_truncated": summary.changed_paths_truncated,
        },
    )
