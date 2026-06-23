from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from crewplane.core.execution_state import RunManifest

from .naming import validate_run_key_name


@dataclass(frozen=True)
class RunHistoryRecord:
    manifest: RunManifest
    manifest_path: Path
    run_dir: Path
    results_dir: Path


class RunHistoryError(RuntimeError):
    """Raised when run history cannot be scanned without unsafe filesystem access."""


def find_same_context_runs(
    state_dir: Path,
    workflow_identity: str,
    workflow_name: str,
    workflow_signature: str,
) -> tuple[RunHistoryRecord, ...]:
    records: list[RunHistoryRecord] = []
    stages_root = state_dir / "execution-stages"
    results_root = state_dir / "execution-results"
    if not _history_root_exists(stages_root):
        return ()
    for run_dir in _candidate_run_dirs(stages_root):
        safe_manifest = _safe_candidate_manifest(stages_root, run_dir)
        if safe_manifest is None:
            continue
        manifest_path = safe_manifest
        manifest = _read_run_manifest(manifest_path)
        if manifest is None:
            continue
        if not _matches_context(
            manifest,
            workflow_identity,
            workflow_name,
            workflow_signature,
        ):
            continue
        record = _history_record_for_manifest(
            manifest,
            manifest_path,
            run_dir,
            stages_root,
            results_root,
        )
        if record is not None:
            records.append(record)
    return tuple(sorted(records, key=_started_at, reverse=True))


def _history_root_exists(stages_root: Path) -> bool:
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
    return stat.S_ISDIR(root_stat.st_mode)


def _read_run_manifest(path: Path) -> RunManifest | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return _validate_run_manifest(payload)


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
) -> RunHistoryRecord | None:
    try:
        run_key_name = validate_run_key_name(manifest.run_key_name)
    except ValueError:
        return None
    if run_dir.name != run_key_name:
        return None
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
    except FileNotFoundError:
        return ()
    except NotADirectoryError:
        return ()
    except PermissionError:
        raise
    except OSError as exc:
        raise RunHistoryError("Cannot inspect run history safely.") from exc


def _safe_candidate_manifest(stages_root: Path, run_dir: Path) -> Path | None:
    manifest_path = run_dir / "manifests" / "run.json"
    try:
        manifest_lstat = manifest_path.lstat()
    except FileNotFoundError:
        return None
    except NotADirectoryError:
        return None
    except PermissionError:
        raise
    except OSError as exc:
        raise RunHistoryError("Cannot inspect run history metadata safely.") from exc
    _ensure_contained_run_path(stages_root, run_dir)
    _ensure_no_symlink_metadata_components(stages_root, manifest_path)
    _ensure_contained_run_path(stages_root, manifest_path)
    if not stat.S_ISREG(manifest_lstat.st_mode) or manifest_lstat.st_nlink != 1:
        raise RunHistoryError("Run history metadata path is not a safe file.")
    return manifest_path


def _contained_run_path(root: Path, run_key_name: str) -> Path:
    candidate = root / run_key_name
    _ensure_contained_run_path(root, candidate)
    return candidate


def _ensure_contained_run_path(root: Path, candidate: Path) -> None:
    try:
        root_resolved = root.resolve(strict=False)
        candidate_resolved = candidate.resolve(strict=False)
    except PermissionError:
        raise
    except OSError as exc:
        raise RunHistoryError("Cannot inspect run history path safely.") from exc
    if not candidate_resolved.is_relative_to(root_resolved):
        raise RunHistoryError("Run history path escapes its expected root.")


def _ensure_no_symlink_metadata_components(root: Path, candidate: Path) -> None:
    if _has_symlink_component(root):
        raise RunHistoryError("Run history metadata path contains a symlink.")
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise RunHistoryError(
            "Run history metadata path escapes its expected root."
        ) from exc
    current = root
    for part in relative.parts:
        current = current / part
        if _path_is_symlink(current):
            raise RunHistoryError("Run history metadata path contains a symlink.")


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor) if path.is_absolute() else Path()
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for part in parts:
        current = current / part
        if _path_is_symlink(current):
            return True
    return False


def _path_is_symlink(path: Path) -> bool:
    try:
        return stat.S_ISLNK(path.lstat().st_mode)
    except PermissionError:
        raise
    except OSError:
        return False


def _started_at(record: RunHistoryRecord) -> datetime:
    return datetime.fromisoformat(record.manifest.started_at)


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
