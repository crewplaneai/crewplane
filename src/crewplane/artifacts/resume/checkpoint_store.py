"""Atomic checkpoint markers and immediate hydration provenance."""

from __future__ import annotations

from pathlib import Path

from crewplane.architecture.safe_file_reads import read_contained_bytes
from crewplane.architecture.safe_files import (
    contained_regular_file,
    ensure_contained_directory,
)
from crewplane.core.execution_state import ReviewCheckpointResumeSummary, RunManifest
from crewplane.core.review_checkpoint import (
    REVIEW_CHECKPOINT_ADAPTER,
    OpenReviewCheckpoint,
    ReviewLoopCheckpoint,
)

from ..atomic import atomic_write_json
from ..naming import review_checkpoint_relative_path, run_manifest_relative_path
from .checkpoint_files import verify_checkpoint_files


def read_review_checkpoint(root: Path, node_id: str) -> ReviewLoopCheckpoint | None:
    relative = review_checkpoint_relative_path(node_id)
    path = contained_regular_file(root, relative.as_posix())
    if path is None:
        if (root / relative).exists() or (root / relative).is_symlink():
            raise ValueError("Checkpoint marker is not a safe regular file.")
        return None
    return REVIEW_CHECKPOINT_ADAPTER.validate_json(
        read_contained_bytes(path.parent, path.name)
    )


def publish_review_checkpoint(root: Path, checkpoint: ReviewLoopCheckpoint) -> Path:
    validated = REVIEW_CHECKPOINT_ADAPTER.validate_python(checkpoint.model_dump())
    if isinstance(validated, OpenReviewCheckpoint):
        verify_checkpoint_files(root, validated.files)
    relative = review_checkpoint_relative_path(validated.node_id)
    parent = ensure_contained_directory(root, relative.parent.as_posix())
    return atomic_write_json(parent / relative.name, validated.model_dump(mode="json"))


def read_checkpoint_provenance(root: Path) -> list[ReviewCheckpointResumeSummary]:
    path = contained_regular_file(root, run_manifest_relative_path().as_posix())
    if path is None:
        return []
    return RunManifest.model_validate_json(
        read_contained_bytes(path.parent, path.name)
    ).resumed_review_checkpoints


def record_checkpoint_provenance(
    root: Path, summary: ReviewCheckpointResumeSummary
) -> Path:
    path = contained_regular_file(root, run_manifest_relative_path().as_posix())
    if path is None:
        raise ValueError("Checkpoint hydration requires a running manifest.")
    manifest = RunManifest.model_validate_json(
        read_contained_bytes(path.parent, path.name)
    )
    if manifest.status != "running":
        raise ValueError("Checkpoint hydration requires a running manifest.")
    payload = manifest.model_dump()
    payload.update(
        resumed_review_checkpoints=[*manifest.resumed_review_checkpoints, summary],
        resume_source_run_id=summary.resume_origin.source_run_id,
        resume_source_run_key_name=summary.resume_origin.source_run_key_name,
    )
    validated = RunManifest.model_validate(payload)
    return atomic_write_json(path, validated.model_dump(mode="json", exclude_none=True))
