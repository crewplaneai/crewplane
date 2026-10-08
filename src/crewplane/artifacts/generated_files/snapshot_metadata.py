"""Read and atomically publish generated-file snapshot metadata."""

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
    """Reported rejection count and recorded details from a snapshot catalog.

    Attributes:
        total_count: Total rejected files, including unrecorded details.
        recorded_files: Detail dictionaries in metadata order, without deduplication.
    """

    total_count: int = 0
    recorded_files: tuple[dict[str, object], ...] = ()

    @property
    def truncated(self) -> bool:
        """Return whether the total count exceeds the number of recorded details."""
        return self.total_count > len(self.recorded_files)


def read_snapshot_source_root(snapshot_root: Path) -> Path | None:
    """Read the recorded source path without resolving or checking its existence.

    Return None for missing or unsafe metadata, protected-read OSError or
    ValueError, invalid JSON, or a source_root that is not a nonempty string.
    Containment lookup errors and other exceptions propagate.
    """
    metadata_file = contained_regular_file(
        snapshot_root,
        GENERATED_FILE_SOURCE_METADATA_NAME,
    )
    if metadata_file is None:
        return None
    payload = _read_metadata_object(snapshot_root, metadata_file)
    if payload is None:
        return None
    source_root = payload.get("source_root")
    if not isinstance(source_root, str) or not source_root:
        return None
    return Path(source_root)


def read_snapshot_candidate_files(
    snapshot_root: Path,
) -> tuple[Path, ...] | None:
    """Return contained single-link regular files in catalog order, with duplicates.

    None means no safe metadata file was found, permitting fallback discovery.
    An unreadable or invalid catalog returns (), restricting candidates to none.
    Skip malformed entries and paths that are not contained regular files.
    OSError and ValueError from protected reading or JSON decoding are handled;
    containment lookup errors and other exceptions propagate.
    """
    metadata_file = contained_regular_file(
        snapshot_root,
        GENERATED_FILE_SNAPSHOT_METADATA_NAME,
    )
    if metadata_file is None:
        return None
    payload = _read_metadata_object(snapshot_root, metadata_file)
    if payload is None:
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
    """Read rejection details in catalog order and normalize their total count.

    Keep dictionary details only. Accept a non-boolean integer total at least as
    large as the recorded detail count; otherwise use that count. Truncation is
    derived from these counts, ignoring the catalog's stored truncation flag.
    Missing, unsafe, invalid, or unreadable metadata returns an empty summary.
    OSError and ValueError from protected reading or JSON decoding are handled;
    containment lookup errors and other exceptions propagate.
    """
    metadata_file = contained_regular_file(
        snapshot_root,
        GENERATED_FILE_SNAPSHOT_METADATA_NAME,
    )
    if metadata_file is None:
        return GeneratedFileSnapshotRejectionSummary()
    payload = _read_metadata_object(snapshot_root, metadata_file)
    if payload is None:
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
    """Atomically publish the source path as POSIX text without resolving it.

    Create missing parent directories and replace existing source metadata.
    Serialize JSON with sorted keys, ASCII escapes, no nonfinite numbers, and
    a trailing LF, then encode as UTF-8. Serialization and publication errors
    propagate; atomic publication owns temporary files and their cleanup.

    Returns:
        (byte_count, SHA-256 hex digest) of the exact bytes published.
    """
    metadata_file = snapshot_root / GENERATED_FILE_SOURCE_METADATA_NAME
    return _write_metadata_object(
        metadata_file,
        {"source_root": source_root.as_posix()},
    )


def write_snapshot_metadata(
    snapshot_root: Path,
    copied_files: Sequence[GeneratedFileSnapshotMetadata],
    rejections: GeneratedFileRejectionLog,
) -> tuple[int, str]:
    """Atomically publish copied files and any rejections in their supplied order.

    Leave inputs unchanged and omit rejection fields when their total is zero.
    Create missing parent directories and replace existing snapshot metadata.
    Serialize JSON with sorted keys, ASCII escapes, no nonfinite numbers, and
    a trailing LF, then encode as UTF-8. Serialization and publication errors
    propagate; atomic publication owns temporary files and their cleanup.

    Returns:
        (byte_count, SHA-256 hex digest) of the exact bytes published.
    """
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
    return _write_metadata_object(metadata_file, payload)


def _read_metadata_object(
    snapshot_root: Path, metadata_file: Path
) -> dict[str, object] | None:
    """Decode a checked metadata file, leaving missing-file policy to the caller."""
    try:
        payload = json.loads(read_contained_bytes(snapshot_root, metadata_file.name))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_metadata_object(
    metadata_file: Path, payload: dict[str, object]
) -> tuple[int, str]:
    """Publish compact JSON and sign the same bytes after publication succeeds."""
    content = json.dumps(payload, allow_nan=False, sort_keys=True) + "\n"
    encoded = content.encode("utf-8")
    atomic_write_bytes(metadata_file, encoded)
    return len(encoded), sha256(encoded).hexdigest()
