from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from crewplane.architecture.safe_files import contained_regular_file
from crewplane.core.execution_state import (
    RUN_STATUS_RUNNING,
    RunManifest,
    TerminalRunStatus,
)

from ..atomic import atomic_write_json
from ..naming import validate_run_key_name


class LockManifestError(RuntimeError):
    """Raised when stale lock manifest metadata cannot be trusted."""


TerminalRecoveryPhase = Literal[
    "outcome_selected",
    "terminal_views_published",
    "observer_shutdown_complete",
]
TERMINAL_RECOVERY_PHASES: tuple[TerminalRecoveryPhase, ...] = (
    "outcome_selected",
    "terminal_views_published",
    "observer_shutdown_complete",
)
_TERMINAL_EVENT_TYPES = frozenset(
    {"workflow_finished", "workflow_failed", "workflow_cancelled"}
)
_TERMINAL_EVENT_TYPE_BY_STATUS: dict[TerminalRunStatus, str] = {
    "succeeded": "workflow_finished",
    "failed": "workflow_failed",
    "cancelled": "workflow_cancelled",
}


class TerminalRecoveryIntent(BaseModel):
    """Selected terminal outcome and its durable publication phase."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    phase: TerminalRecoveryPhase
    status: TerminalRunStatus
    reason: str | None = None

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Terminal recovery reason cannot be blank.")
        return value

    @model_validator(mode="after")
    def _validate_status_reason(self) -> TerminalRecoveryIntent:
        if self.status == "succeeded" and self.reason is not None:
            raise ValueError("Successful terminal recovery cannot include a reason.")
        if self.status != "succeeded" and self.reason is None:
            raise ValueError("Failed and cancelled terminal recovery require a reason.")
        return self


@dataclass(frozen=True)
class LockRunMetadata:
    run_id: str | None
    run_key_name: str | None
    workflow_identity: str
    workflow_signature: str
    terminal_recovery: TerminalRecoveryIntent | None = None


def finalize_stale_running_run(
    state_dir: Path,
    metadata: LockRunMetadata,
) -> None:
    if _stale_run_owner_metadata_is_absent(metadata):
        return

    run_key_name = _stale_run_key_name(metadata)

    manifest_path = safe_owner_manifest_path(state_dir, run_key_name)
    if manifest_path is None:
        return
    manifest = read_owner_manifest(manifest_path)

    validate_owner_manifest_match(metadata, manifest)
    if manifest.status != RUN_STATUS_RUNNING:
        return

    status, reason = _stale_terminal_outcome(
        state_dir,
        manifest,
        metadata.terminal_recovery,
    )
    _write_stale_terminal_manifest(state_dir, run_key_name, manifest, status, reason)


def _stale_run_owner_metadata_is_absent(metadata: LockRunMetadata) -> bool:
    return metadata.run_id is None and metadata.run_key_name is None


def _stale_run_key_name(metadata: LockRunMetadata) -> str:
    if metadata.run_id is None or metadata.run_key_name is None:
        raise LockManifestError("Lock owner run metadata is incomplete.")
    return metadata.run_key_name


def _write_stale_terminal_manifest(
    state_dir: Path,
    run_key_name: str,
    manifest: RunManifest,
    status: TerminalRunStatus,
    reason: str | None,
) -> None:
    updated = manifest.model_copy(update=_stale_manifest_updates(status, reason))
    validated = RunManifest.model_validate(updated.model_dump(mode="json"))
    manifest_path = safe_owner_manifest_path(state_dir, run_key_name)
    if manifest_path is None:
        return
    atomic_write_json(
        manifest_path,
        validated.model_dump(mode="json", exclude_none=True),
    )


def _stale_manifest_updates(
    status: TerminalRunStatus,
    reason: str | None,
) -> dict[str, object]:
    updates: dict[str, object] = {
        "status": status,
        "completed_at": datetime.now().isoformat(),
        "failure_message": None,
        "cancel_reason": None,
    }
    if status == "failed":
        updates["failure_message"] = reason
    elif status == "cancelled":
        updates["cancel_reason"] = reason
    return updates


def _needs_stale_terminal_default(
    state_dir: Path,
    manifest: RunManifest,
    recovery: TerminalRecoveryIntent,
) -> bool:
    return recovery.phase == "outcome_selected" and not _terminal_views_match(
        state_dir,
        manifest,
        recovery,
    )


def _stale_terminal_outcome(
    state_dir: Path,
    manifest: RunManifest,
    recovery: TerminalRecoveryIntent | None,
) -> tuple[TerminalRunStatus, str | None]:
    if recovery is None:
        return "cancelled", "stale_lock_recovered"
    if _needs_stale_terminal_default(state_dir, manifest, recovery):
        return "cancelled", "stale_lock_recovered"
    return recovery.status, recovery.reason


def _terminal_views_match(
    state_dir: Path,
    manifest: RunManifest,
    recovery: TerminalRecoveryIntent,
) -> bool:
    try:
        event_log_path, summary_path = _terminal_view_file_paths(state_dir, manifest)
        if event_log_path is None or summary_path is None:
            return False
        event_log = event_log_path.read_text(encoding="utf-8")
        summary = summary_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    return _terminal_event_matches(event_log, manifest, recovery) and (
        _terminal_summary_matches(summary, manifest, recovery.status)
    )


def _terminal_view_file_paths(
    state_dir: Path,
    manifest: RunManifest,
) -> tuple[Path | None, Path | None]:
    stages_root = state_dir / "execution-stages"
    log_prefix = f"{manifest.run_key_name}/logs"
    return (
        contained_regular_file(stages_root, f"{log_prefix}/events.ndjson"),
        contained_regular_file(stages_root, f"{log_prefix}/summary.md"),
    )


def _terminal_event_matches(
    event_log: str,
    manifest: RunManifest,
    recovery: TerminalRecoveryIntent,
) -> bool:
    terminal_records = _extract_terminal_event_records(event_log)
    if terminal_records is None:
        return False
    if len(terminal_records) != 1:
        return False
    return _terminal_record_matches(terminal_records[0], manifest, recovery)


def _extract_terminal_event_records(event_log: str) -> list[dict[str, object]] | None:
    records = _event_records(event_log)
    if records is None:
        return None
    return [
        record
        for record in records
        if record.get("event_type") in _TERMINAL_EVENT_TYPES
    ]


def _terminal_record_matches(
    terminal: dict[str, object],
    manifest: RunManifest,
    recovery: TerminalRecoveryIntent,
) -> bool:
    timestamp = terminal.get("timestamp")
    return (
        terminal.get("event_type") == _TERMINAL_EVENT_TYPE_BY_STATUS[recovery.status]
        and terminal.get("workflow_name") == manifest.workflow_name
        and terminal.get("run_id") == manifest.run_id
        and terminal.get("error") == recovery.reason
        and isinstance(timestamp, str)
        and bool(timestamp.strip())
    )


def _event_records(event_log: str) -> list[dict[str, object]] | None:
    records: list[dict[str, object]] = []
    for line in event_log.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(record, dict):
            return None
        records.append(record)
    return records


def _terminal_summary_matches(
    summary: str,
    manifest: RunManifest,
    status: TerminalRunStatus,
) -> bool:
    lines = summary.splitlines()
    expected_status = _terminal_summary_status_line(status)
    expected_header = _terminal_summary_expected_header(manifest, expected_status)
    return lines[
        : len(expected_header)
    ] == expected_header and _terminal_summary_status_lines(summary) == [
        expected_status
    ]


def _terminal_summary_expected_header(
    manifest: RunManifest,
    expected_status: str,
) -> list[str]:
    return [
        "# Run Summary",
        "",
        f"- Workflow: {manifest.workflow_name}",
        f"- Run ID: {manifest.run_id}",
        expected_status,
    ]


def _terminal_summary_status_line(status: TerminalRunStatus) -> str:
    return f"- Status: {status}"


def _terminal_summary_status_lines(summary: str) -> list[str]:
    return [line for line in summary.splitlines() if line.startswith("- Status: ")]


def read_owner_manifest(manifest_path: Path) -> RunManifest:
    try:
        return RunManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except PermissionError:
        raise
    except (OSError, ValueError) as exc:
        raise LockManifestError(
            "Stale run manifest is malformed or unreadable."
        ) from exc


def validate_owner_manifest_match(
    metadata: LockRunMetadata,
    manifest: RunManifest,
) -> None:
    if (
        manifest.run_id != metadata.run_id
        or manifest.run_key_name != metadata.run_key_name
        or manifest.workflow_identity != metadata.workflow_identity
        or manifest.workflow_signature != metadata.workflow_signature
    ):
        raise LockManifestError("Lock owner run metadata does not match run manifest.")


def safe_owner_manifest_path(
    state_dir: Path,
    run_key_name: str,
) -> Path | None:
    stages_root = state_dir / "execution-stages"
    manifest_path = owner_manifest_path(state_dir, run_key_name)
    if manifest_path is None:
        raise LockManifestError("Lock owner run metadata is not safely contained.")
    ensure_no_symlink_manifest_components(stages_root, manifest_path)
    ensure_owner_path_contained(stages_root, manifest_path)
    try:
        manifest_lstat = manifest_path.lstat()
    except FileNotFoundError:
        return None
    except NotADirectoryError:
        return None
    except PermissionError:
        raise
    except OSError as exc:
        raise LockManifestError("Cannot inspect stale run manifest safely.") from exc

    _ensure_safe_file(manifest_lstat)
    return manifest_path


def _ensure_safe_file(manifest_lstat: os.stat_result) -> None:
    if not stat.S_ISREG(manifest_lstat.st_mode) or manifest_lstat.st_nlink != 1:
        raise LockManifestError("Stale run manifest is not a safe file.")


def owner_manifest_path(state_dir: Path, run_key_name: str) -> Path | None:
    try:
        run_key_name = validate_run_key_name(run_key_name)
    except ValueError as exc:
        raise LockManifestError(
            "Lock owner run metadata is not safely contained."
        ) from exc
    stages_root = state_dir / "execution-stages"
    run_dir = stages_root / run_key_name
    try:
        stages_root_resolved = stages_root.resolve(strict=False)
        run_dir_resolved = run_dir.resolve(strict=False)
    except PermissionError:
        raise
    except OSError:
        return None
    if not run_dir_resolved.is_relative_to(stages_root_resolved):
        return None
    return run_dir / "manifests" / "run.json"


def ensure_owner_path_contained(root: Path, candidate: Path) -> None:
    try:
        root_resolved = root.resolve(strict=False)
        candidate_resolved = candidate.resolve(strict=False)
    except PermissionError:
        raise
    except OSError as exc:
        raise LockManifestError("Cannot inspect stale run manifest safely.") from exc
    if not candidate_resolved.is_relative_to(root_resolved):
        raise LockManifestError("Lock owner run metadata is not safely contained.")


def ensure_no_symlink_manifest_components(root: Path, candidate: Path) -> None:
    if has_symlink_component(root):
        raise LockManifestError("Stale run manifest path contains a symlink.")
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise LockManifestError(
            "Lock owner run metadata is not safely contained."
        ) from exc
    current = root
    for part in relative.parts:
        current = current / part
        if path_is_symlink(current):
            raise LockManifestError("Stale run manifest path contains a symlink.")


def has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor) if path.is_absolute() else Path()
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for part in parts:
        current = current / part
        if path_is_symlink(current):
            return True
    return False


def path_is_symlink(path: Path) -> bool:
    try:
        return stat.S_ISLNK(path.lstat().st_mode)
    except PermissionError:
        raise
    except OSError:
        return False
