"""Preserve explicit generated-file mappings, including failed captures."""

from __future__ import annotations

import json
from pathlib import Path

from crewplane.architecture.safe_files import contained_regular_file
from crewplane.core.review_checkpoint_state import (
    CheckpointFile,
    CheckpointGeneratedMapping,
    CheckpointInvocation,
)

from ..generated_files.evidence import verified_generated_file_descriptors
from ..generated_files.paths import (
    GENERATED_FILE_SNAPSHOT_METADATA_NAME,
    GENERATED_FILE_SOURCE_METADATA_NAME,
)
from .checkpoint_files import describe_checkpoint_file


def describe_generated_mapping(
    root: Path,
    output_path: str,
    snapshot_root: Path | None,
    invocation: CheckpointInvocation,
) -> tuple[CheckpointGeneratedMapping, list[CheckpointFile]]:
    """Describe a generated-file capture without modifying its evidence.

    A None snapshot records an explicit failed capture and needs no filesystem
    access. Existing snapshots may be partial, including captures with no files;
    their source metadata must name an absolute root, which need not still exist.
    The output itself is not read or described.

    Args:
        root: Run stage root containing the snapshot.
        output_path: Normalized relative POSIX path of the associated output.
        snapshot_root: Captured workspace below root, or None for a failed capture.
        invocation: Task, role, audit, and local round for snapshot descriptors.

    Returns:
        The mapping and its dependency descriptors, or an empty list for a failed
        capture. Descriptors name source metadata, snapshot metadata, then captured
        files in sorted path order. Captured file signatures are rechecked during
        descriptor construction; reads acquire no snapshot-wide lock.

    Raises:
        ValueError: The snapshot is outside root, evidence is unavailable, source
            metadata lacks an absolute source_root, or a captured file changes.
            Containment and evidence availability precede source JSON validation;
            descriptor checks follow in return order.
        pydantic.ValidationError: Mapping paths or descriptor metadata are invalid.
        json.JSONDecodeError: Source metadata is malformed JSON.
        UnicodeDecodeError: Source metadata has an invalid JSON encoding.
        OSError: File inspection, source reading, or descriptor hashing fails
            outside the snapshot evidence verifier's handled failures.
    """
    if snapshot_root is None:
        return CheckpointGeneratedMapping(
            output_path=output_path, snapshot_path=None
        ), []
    relative = snapshot_root.relative_to(root).as_posix()
    entries = _verify_generated_snapshot_evidence(snapshot_root)
    files = _describe_generated_snapshot_files(root, relative, invocation, entries)
    return CheckpointGeneratedMapping(
        output_path=output_path, snapshot_path=relative
    ), files


def _verify_generated_snapshot_evidence(
    snapshot_root: Path,
) -> list[tuple[str, int, str]]:
    entries = verified_generated_file_descriptors(
        snapshot_root, require_complete_capture=False
    )
    source = contained_regular_file(snapshot_root, GENERATED_FILE_SOURCE_METADATA_NAME)
    if entries is None or source is None:
        raise ValueError("Checkpoint generated-file snapshot evidence is unavailable.")
    metadata = json.loads(source.read_bytes())
    if (
        not isinstance(metadata, dict)
        or not isinstance(metadata.get("source_root"), str)
        or not Path(metadata["source_root"]).is_absolute()
    ):
        raise ValueError("Checkpoint generated-file source metadata is invalid.")
    return entries


def _describe_generated_snapshot_files(
    root: Path,
    relative: str,
    invocation: CheckpointInvocation,
    entries: list[tuple[str, int, str]],
) -> list[CheckpointFile]:
    files = [
        describe_checkpoint_file(
            root, f"{relative}/{name}", invocation, "generated_metadata"
        )
        for name in (
            GENERATED_FILE_SOURCE_METADATA_NAME,
            GENERATED_FILE_SNAPSHOT_METADATA_NAME,
        )
    ]
    files.extend(
        describe_checkpoint_file(
            root, f"{relative}/{name}", invocation, "generated_file", (size, digest)
        )
        for name, size, digest in entries
    )
    return files


def validate_generated_mappings(
    root: Path, mappings: list[CheckpointGeneratedMapping], files: list[CheckpointFile]
) -> set[str]:
    """Match snapshot dependencies to carried descriptors in mapping order.

    The caller must validate checkpoint metadata and verify carried file bytes
    first. Each snapshot-backed mapping requires a carried output descriptor,
    whose invocation identifies the expected snapshot descriptors. Failed captures
    are skipped without requiring an output descriptor. Partial snapshots are
    accepted under describe_generated_mapping's evidence rules.

    Returns:
        The set of snapshot metadata and captured file paths, excluding output
        paths; empty when no mapping carries a snapshot. Files and descriptors
        remain unchanged. Output bytes, unrelated dependencies, and completeness
        of the checkpoint's dependency list are not checked here.

    Raises:
        ValueError: A snapshot-backed output is not carried, expected descriptors
            differ from carried descriptors, or snapshot evidence is invalid.
            Missing outputs fail before snapshot inspection; the first failing
            mapping stops validation.
        pydantic.ValidationError: Invocation, mapping, or descriptor metadata is
            invalid.
        json.JSONDecodeError: Snapshot source metadata is malformed JSON.
        UnicodeDecodeError: Snapshot source metadata has an invalid JSON encoding.
        OSError: An underlying snapshot inspection, read, or hash error propagates.
    """
    paths: set[str] = set()
    descriptors = {item.relative_path: item for item in files}
    for mapping in mappings:
        if mapping.snapshot_path is None:
            continue
        output = descriptors.get(mapping.output_path)
        if output is None:
            raise ValueError("Generated mapping output is not carried.")
        invocation = CheckpointInvocation(
            task_id=output.task_id,
            role=output.role,
            audit=output.audit,
            local_round=output.local_round,
        )
        _, expected = describe_generated_mapping(
            root, mapping.output_path, root / mapping.snapshot_path, invocation
        )
        if any(descriptors.get(item.relative_path) != item for item in expected):
            raise ValueError(
                "Generated snapshot dependencies do not match their mapping."
            )
        paths.update(item.relative_path for item in expected)
    return paths
