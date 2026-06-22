from __future__ import annotations

import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from orchestrator_cli.core.execution_state import (
    RUN_STATUS_CANCELLED,
    RUN_STATUS_RUNNING,
    RunManifest,
)

from ..atomic import atomic_write_json
from ..naming import validate_run_key_name


class LockManifestError(RuntimeError):
    """Raised when stale lock manifest metadata cannot be trusted."""


@dataclass(frozen=True)
class LockRunMetadata:
    run_id: str | None
    run_key_name: str | None
    workflow_identity: str
    workflow_signature: str


def finalize_stale_running_run(
    orchestrator_dir: Path,
    metadata: LockRunMetadata,
) -> None:
    if metadata.run_id is None and metadata.run_key_name is None:
        return
    if metadata.run_id is None or metadata.run_key_name is None:
        raise LockManifestError("Lock owner run metadata is incomplete.")
    manifest_path = safe_owner_manifest_path(orchestrator_dir, metadata.run_key_name)
    if manifest_path is None:
        return
    manifest = read_owner_manifest(manifest_path)
    validate_owner_manifest_match(metadata, manifest)
    if manifest.status != RUN_STATUS_RUNNING:
        return
    updated = manifest.model_copy(
        update={
            "status": RUN_STATUS_CANCELLED,
            "completed_at": datetime.now().isoformat(),
            "cancel_reason": "stale_lock_recovered",
        }
    )
    validated = RunManifest.model_validate(updated.model_dump(mode="json"))
    manifest_path = safe_owner_manifest_path(orchestrator_dir, metadata.run_key_name)
    if manifest_path is None:
        return
    atomic_write_json(
        manifest_path,
        validated.model_dump(mode="json", exclude_none=True),
    )


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
    orchestrator_dir: Path,
    run_key_name: str,
) -> Path | None:
    stages_root = orchestrator_dir / "execution-stages"
    manifest_path = owner_manifest_path(orchestrator_dir, run_key_name)
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
    if not stat.S_ISREG(manifest_lstat.st_mode) or manifest_lstat.st_nlink != 1:
        raise LockManifestError("Stale run manifest is not a safe file.")
    return manifest_path


def owner_manifest_path(orchestrator_dir: Path, run_key_name: str) -> Path | None:
    try:
        run_key_name = validate_run_key_name(run_key_name)
    except ValueError as exc:
        raise LockManifestError(
            "Lock owner run metadata is not safely contained."
        ) from exc
    stages_root = orchestrator_dir / "execution-stages"
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
