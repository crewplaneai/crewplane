from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from crewplane.architecture.safe_file_reads import read_contained_bytes
from crewplane.architecture.safe_files import contained_regular_file
from crewplane.artifacts.atomic import atomic_write_bytes

from .paths import (
    GENERATED_FILE_SNAPSHOT_METADATA_NAME,
    GENERATED_FILE_SOURCE_METADATA_NAME,
)
from .snapshot_policy import GeneratedFileRejectionLog, GeneratedFileSnapshotMetadata


@dataclass(frozen=True)
class GeneratedFileSnapshotRejectionSummary:
    total_count: int = 0
    recorded_files: tuple[dict[str, object], ...] = ()

    @property
    def truncated(self) -> bool:
        return self.total_count > len(self.recorded_files)


def read_snapshot_source_root(snapshot_root: Path) -> Path | None:
    metadata_file = contained_regular_file(
        snapshot_root,
        GENERATED_FILE_SOURCE_METADATA_NAME,
    )
    if metadata_file is None:
        return None
    try:
        payload = json.loads(read_contained_bytes(snapshot_root, metadata_file.name))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    source_root = payload.get("source_root")
    if not isinstance(source_root, str) or not source_root:
        return None
    return Path(source_root)


def read_snapshot_candidate_files(
    snapshot_root: Path,
) -> tuple[Path, ...] | None:
    metadata_file = contained_regular_file(
        snapshot_root,
        GENERATED_FILE_SNAPSHOT_METADATA_NAME,
    )
    if metadata_file is None:
        return None
    try:
        payload = json.loads(read_contained_bytes(snapshot_root, metadata_file.name))
    except (OSError, ValueError):
        return ()
    if not isinstance(payload, dict):
        return ()
    raw_files = payload.get("files")
    if not isinstance(raw_files, list):
        return ()
    candidates: list[Path] = []
    for item in raw_files:
        if not isinstance(item, dict):
            continue
        raw_path = item.get("path")
        if not isinstance(raw_path, str):
            continue
        candidate = contained_regular_file(snapshot_root, raw_path)
        if candidate is not None:
            candidates.append(candidate)
    return tuple(candidates)


def generated_file_snapshot_rejection_summary(
    snapshot_root: Path,
) -> GeneratedFileSnapshotRejectionSummary:
    metadata_file = contained_regular_file(
        snapshot_root,
        GENERATED_FILE_SNAPSHOT_METADATA_NAME,
    )
    if metadata_file is None:
        return GeneratedFileSnapshotRejectionSummary()
    try:
        payload = json.loads(read_contained_bytes(snapshot_root, metadata_file.name))
    except (OSError, ValueError):
        return GeneratedFileSnapshotRejectionSummary()
    if not isinstance(payload, dict):
        return GeneratedFileSnapshotRejectionSummary()
    raw_rejections = payload.get("rejected_files")
    if not isinstance(raw_rejections, list):
        raw_rejections = []
    recorded_files = tuple(item for item in raw_rejections if isinstance(item, dict))
    raw_total_count = payload.get("rejected_file_count")
    total_count = (
        raw_total_count
        if isinstance(raw_total_count, int)
        and not isinstance(raw_total_count, bool)
        and raw_total_count >= len(recorded_files)
        else len(recorded_files)
    )
    return GeneratedFileSnapshotRejectionSummary(
        total_count=total_count,
        recorded_files=recorded_files,
    )


def write_source_metadata(
    snapshot_root: Path,
    source_root: Path,
) -> tuple[int, str]:
    metadata_file = snapshot_root / GENERATED_FILE_SOURCE_METADATA_NAME
    content = (
        json.dumps(
            {"source_root": source_root.as_posix()},
            allow_nan=False,
            sort_keys=True,
        )
        + "\n"
    )
    payload = content.encode("utf-8")
    atomic_write_bytes(metadata_file, payload)
    return len(payload), sha256(payload).hexdigest()


def write_snapshot_metadata(
    snapshot_root: Path,
    copied_files: Sequence[GeneratedFileSnapshotMetadata],
    rejections: GeneratedFileRejectionLog,
) -> tuple[int, str]:
    payload: dict[str, object] = {"files": list(copied_files)}
    if rejections.rejected_file_count:
        payload.update(
            {
                "rejected_file_count": rejections.rejected_file_count,
                "rejected_files": list(rejections.rejected_files),
                "rejected_files_truncated": rejections.truncated,
            }
        )
    metadata_file = snapshot_root / GENERATED_FILE_SNAPSHOT_METADATA_NAME
    content = json.dumps(payload, allow_nan=False, sort_keys=True) + "\n"
    encoded = content.encode("utf-8")
    atomic_write_bytes(metadata_file, encoded)
    return len(encoded), sha256(encoded).hexdigest()
