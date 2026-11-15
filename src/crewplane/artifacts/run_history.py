"""Discover persisted execution runs without modifying their artifact trees."""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from crewplane.architecture.safe_file_reads import read_contained_bytes
from crewplane.architecture.safe_files import (
    is_single_link_regular_file,
    path_has_symlink_component,
    relative_path_has_symlink,
    resolved_path_is_contained,
)
from crewplane.core.execution_state import RunManifest
from crewplane.core.state_paths import (
    EXECUTION_RESULTS_DIR_NAME,
    EXECUTION_STAGES_DIR_NAME,
)

from .naming import run_manifest_relative_path, validate_run_key_name

type _ManifestCategory = Literal["invalid_json", "invalid_utf8", "invalid_manifest"]
type _KeyCategory = Literal["invalid_key", "key_mismatch"]
type _DiagnosticCategory = _ManifestCategory | _KeyCategory | Literal["no_manifest"]


@dataclass(frozen=True)
class RunHistoryRecord:
    """A validated manifest and contained artifact paths for one execution.

    The results directory may be absent; discovery does not create it or verify
    node outputs for reuse.
    """

    manifest: RunManifest
    manifest_path: Path
    run_dir: Path
    results_dir: Path


@dataclass(frozen=True)
class RunHistoryDiagnostic:
    """An excluded candidate's manifest path, category, and readable reason.

    Missing manifests and invalid content produce diagnostics. Unsafe metadata
    and filesystem failures instead propagate from the scanner.
    """

    run_key_name: str
    path: Path
    category: str
    message: str


@dataclass(frozen=True)
class RunHistoryScan:
    """Catalogue records newest first, with run-key ties in ascending order.

    Diagnostics follow candidate path order. Either tuple may be empty.
    """

    records: tuple[RunHistoryRecord, ...]
    diagnostics: tuple[RunHistoryDiagnostic, ...]


def persisted_start_time(value: str) -> datetime:
    """Compare recorded instants, interpreting naive values as local wall time."""
    return datetime.fromisoformat(value).astimezone(UTC)


def scan_run_history(state_dir: Path) -> RunHistoryScan:
    """Read all workflow contexts, newest recorded instant first.

    Naive start times represent local wall time; equal instants sort by run key.
    Directories with missing or invalid manifests, unrepresentable UTC start
    times, or invalid/mismatched keys produce diagnostics in candidate path
    order. Regular entries without manifests are ignored. A missing stages
    root returns an empty scan, and absent results directories remain absent.

    Raises:
        RunHistoryError: Metadata is unsafe, changes during reading, or cannot
            be inspected safely, including existing non-directory roots.
        OSError: An unhandled filesystem error, including permission failures.
    """
    stages_root = state_dir / EXECUTION_STAGES_DIR_NAME
    if not _history_root_exists(stages_root):
        return RunHistoryScan((), ())
    records: list[RunHistoryRecord] = []
    diagnostics: list[RunHistoryDiagnostic] = []
    for run_dir in sorted(_candidate_run_dirs(stages_root)):
        candidate = _scan_history_candidate(state_dir, run_dir)
        if isinstance(candidate, RunHistoryRecord):
            records.append(candidate)
        elif isinstance(candidate, RunHistoryDiagnostic):
            diagnostics.append(candidate)
    records.sort(key=lambda record: record.manifest.run_key_name)
    records.sort(
        key=lambda record: persisted_start_time(record.manifest.started_at),
        reverse=True,
    )
    return RunHistoryScan(tuple(records), tuple(diagnostics))


def _scan_history_candidate(
    state_dir: Path, run_dir: Path
) -> RunHistoryRecord | RunHistoryDiagnostic | None:
    stages_root = state_dir / EXECUTION_STAGES_DIR_NAME
    _ensure_contained_run_path(stages_root, run_dir)
    _ensure_no_symlink_metadata_components(
        stages_root, run_dir / run_manifest_relative_path()
    )
    manifest_path = _safe_candidate_manifest(stages_root, run_dir)
    if manifest_path is None:
        return _diagnostic(run_dir, "no_manifest") if run_dir.is_dir() else None
    manifest = _read_manifest_evidence(manifest_path)
    if not isinstance(manifest, RunManifest):
        return _diagnostic(run_dir, manifest)
    try:
        persisted_start_time(manifest.started_at)
    except (OverflowError, ValueError):
        return _diagnostic(run_dir, "invalid_manifest")
    record = _history_record_for_manifest(
        manifest,
        manifest_path,
        run_dir,
        stages_root,
        state_dir / EXECUTION_RESULTS_DIR_NAME,
    )
    return (
        record if isinstance(record, RunHistoryRecord) else _diagnostic(run_dir, record)
    )


def _diagnostic(run_dir: Path, category: _DiagnosticCategory) -> RunHistoryDiagnostic:
    reasons: dict[_DiagnosticCategory, str] = {
        "no_manifest": "No execution manifest; possible preflight or incomplete attempt.",
        "invalid_json": "Execution manifest is not valid JSON.",
        "invalid_utf8": "Execution manifest is not valid UTF-8.",
        "invalid_manifest": "Execution manifest fails current-schema validation.",
        "invalid_key": "Execution manifest has an invalid run key.",
        "key_mismatch": "Execution manifest run key differs from its directory.",
    }
    return RunHistoryDiagnostic(
        run_dir.name,
        run_dir / run_manifest_relative_path(),
        category,
        reasons[category],
    )


def _key_diagnostic(manifest: RunManifest, run_dir: Path) -> _KeyCategory | None:
    try:
        validate_run_key_name(manifest.run_key_name)
    except ValueError:
        return "invalid_key"
    return "key_mismatch" if run_dir.name != manifest.run_key_name else None


class RunHistoryError(RuntimeError):
    """Raised when run history cannot be scanned without unsafe filesystem access."""


def find_same_context_runs(
    state_dir: Path,
    workflow_identity: str,
    workflow_name: str,
    workflow_signature: str,
) -> tuple[RunHistoryRecord, ...]:
    """Read matching identity/name/signature runs, newest start time first.

    Naive-only history sorts by wall time. Mixed timestamps interpret naive
    values as local time; equal keys retain filesystem enumeration order.
    Missing or malformed manifests and invalid/mismatched keys are skipped
    without diagnostics. Context filtering precedes key and
    results-path validation. A missing stages root returns an empty tuple, and
    absent results directories remain absent.

    Raises:
        RunHistoryError: Metadata is unsafe, changes during reading, or cannot
            be inspected safely, including existing non-directory roots.
        OSError: An unhandled filesystem error, including permission failures.
        ValueError: A mixed history's naive start time cannot be interpreted
            as local time.
        OverflowError: A mixed history's naive start time cannot convert to UTC.
    """
    records: list[RunHistoryRecord] = []
    stages_root = state_dir / EXECUTION_STAGES_DIR_NAME
    if not _history_root_exists(stages_root):
        return ()
    for run_dir in _candidate_run_dirs(stages_root):
        record = _same_context_candidate(
            state_dir,
            run_dir,
            workflow_identity,
            workflow_name,
            workflow_signature,
        )
        if record is not None:
            records.append(record)
    return _sort_same_context_records(records)


def _same_context_candidate(
    state_dir: Path,
    run_dir: Path,
    workflow_identity: str,
    workflow_name: str,
    workflow_signature: str,
) -> RunHistoryRecord | None:
    stages_root = state_dir / EXECUTION_STAGES_DIR_NAME
    manifest_path = _safe_candidate_manifest(stages_root, run_dir)
    if manifest_path is None:
        return None
    manifest = _read_manifest_evidence(manifest_path)
    if not isinstance(manifest, RunManifest):
        return None
    if not _matches_context(
        manifest, workflow_identity, workflow_name, workflow_signature
    ):
        return None
    record = _history_record_for_manifest(
        manifest,
        manifest_path,
        run_dir,
        stages_root,
        state_dir / EXECUTION_RESULTS_DIR_NAME,
    )
    return record if isinstance(record, RunHistoryRecord) else None


def _history_root_exists(stages_root: Path) -> bool:
    if path_has_symlink_component(stages_root):
        raise RunHistoryError("Run history root contains a symlink.")
    try:
        root_stat = stages_root.lstat()
    except FileNotFoundError:
        return False
    except PermissionError:
        raise
    except OSError as exc:
        raise RunHistoryError("Cannot inspect run history safely.") from exc
    if stat.S_ISLNK(root_stat.st_mode):
        raise RunHistoryError("Run history root contains a symlink.")
    if not stat.S_ISDIR(root_stat.st_mode):
        raise RunHistoryError("Run history root is not a directory.")
    return True


def _read_manifest_evidence(path: Path) -> RunManifest | _ManifestCategory:
    try:
        raw = read_contained_bytes(path.parent, path.name)
        _ensure_no_symlink_metadata_components(path.parent, path)
    except (FileNotFoundError, ValueError, RunHistoryError) as exc:
        raise RunHistoryError(
            f"Execution manifest changed or became unsafe while reading {path}; retry."
        ) from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return "invalid_utf8"
    except (ValueError, RecursionError):
        return "invalid_json"
    manifest = _validate_run_manifest(payload)
    return manifest if manifest is not None else "invalid_manifest"


def _validate_run_manifest(payload: object) -> RunManifest | None:
    try:
        return RunManifest.model_validate(payload)
    except ValidationError:
        return None


def _history_record_for_manifest(
    manifest: RunManifest,
    manifest_path: Path,
    run_dir: Path,
    stages_root: Path,
    results_root: Path,
) -> RunHistoryRecord | _KeyCategory:
    category = _key_diagnostic(manifest, run_dir)
    if category is not None:
        return category
    run_key_name = manifest.run_key_name
    _contained_run_path(stages_root, run_key_name)
    results_dir = _contained_run_path(results_root, run_key_name)
    return RunHistoryRecord(
        manifest=manifest,
        manifest_path=manifest_path,
        run_dir=run_dir,
        results_dir=results_dir,
    )


def _candidate_run_dirs(stages_root: Path) -> tuple[Path, ...]:
    try:
        return tuple(stages_root.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return ()
    except PermissionError:
        raise
    except OSError as exc:
        raise RunHistoryError("Cannot inspect run history safely.") from exc


def _safe_candidate_manifest(stages_root: Path, run_dir: Path) -> Path | None:
    manifest_path = run_dir / run_manifest_relative_path()
    try:
        manifest_lstat = manifest_path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except PermissionError:
        raise
    except OSError as exc:
        raise RunHistoryError("Cannot inspect run history metadata safely.") from exc
    _ensure_contained_run_path(stages_root, run_dir)
    _ensure_no_symlink_metadata_components(stages_root, manifest_path)
    _ensure_contained_run_path(stages_root, manifest_path)
    if not is_single_link_regular_file(manifest_lstat):
        raise RunHistoryError("Run history metadata path is not a safe file.")
    return manifest_path


def _contained_run_path(root: Path, run_key_name: str) -> Path:
    candidate = root / run_key_name
    _ensure_contained_run_path(root, candidate)
    _ensure_no_symlink_metadata_components(root, candidate)
    _history_root_exists(root)
    _history_root_exists(candidate)
    return candidate


def _ensure_contained_run_path(root: Path, candidate: Path) -> None:
    try:
        contained = resolved_path_is_contained(root, candidate)
    except PermissionError:
        raise
    except OSError as exc:
        raise RunHistoryError("Cannot inspect run history path safely.") from exc
    if not contained:
        raise RunHistoryError("Run history path escapes its expected root.")


def _ensure_no_symlink_metadata_components(root: Path, candidate: Path) -> None:
    if path_has_symlink_component(root):
        raise RunHistoryError("Run history metadata path contains a symlink.")
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise RunHistoryError(
            "Run history metadata path escapes its expected root."
        ) from exc
    if relative_path_has_symlink(root, relative):
        raise RunHistoryError("Run history metadata path contains a symlink.")


def _sort_same_context_records(
    records: list[RunHistoryRecord],
) -> tuple[RunHistoryRecord, ...]:
    start_times = [
        datetime.fromisoformat(record.manifest.started_at) for record in records
    ]
    if any(started_at.tzinfo is not None for started_at in start_times):
        start_times = [
            started_at.astimezone(UTC) if started_at.tzinfo is None else started_at
            for started_at in start_times
        ]
    return tuple(
        record
        for _, record in sorted(
            zip(start_times, records, strict=True),
            key=lambda candidate: candidate[0],
            reverse=True,
        )
    )


def _matches_context(
    manifest: RunManifest,
    workflow_identity: str,
    workflow_name: str,
    workflow_signature: str,
) -> bool:
    return (
        manifest.workflow_identity == workflow_identity
        and manifest.workflow_name == workflow_name
        and manifest.workflow_signature == workflow_signature
    )
