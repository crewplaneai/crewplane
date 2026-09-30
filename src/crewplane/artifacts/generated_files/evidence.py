from __future__ import annotations

import json
from pathlib import Path

from crewplane.architecture.safe_files import contained_regular_file
from crewplane.core.file_hashing import file_size_and_sha256

from .paths import GENERATED_FILE_SNAPSHOT_METADATA_NAME


def verified_generated_file_descriptors(
    root: Path,
) -> list[tuple[str, int, str]] | None:
    """Return verified captured-file evidence, or None when it is unavailable."""
    entries = _read_generated_file_entries(root)
    if entries is None:
        return None
    descriptors = []
    for entry in entries:
        descriptor = _verified_generated_file_descriptor(root, entry)
        if descriptor is None:
            return None
        descriptors.append(descriptor)
    return sorted(descriptors)


def _read_generated_file_entries(root: Path) -> list[object] | None:
    metadata = contained_regular_file(root, GENERATED_FILE_SNAPSHOT_METADATA_NAME)
    if metadata is None:
        return None
    try:
        payload: object = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("rejected_file_count", 0):
        return None
    files = payload.get("files")
    return files if isinstance(files, list) else None


def _verified_generated_file_descriptor(
    root: Path, entry: object
) -> tuple[str, int, str] | None:
    if not isinstance(entry, dict):
        return None
    relative_path = entry.get("path")
    if not isinstance(relative_path, str):
        return None
    try:
        path = contained_regular_file(root, relative_path)
        if path is None:
            return None
        size, digest = file_size_and_sha256(path)
    except (OSError, ValueError):
        return None
    if size != entry.get("size_bytes"):
        return None
    return relative_path, size, digest
