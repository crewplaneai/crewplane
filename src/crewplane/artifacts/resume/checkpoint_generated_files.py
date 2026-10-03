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
    if snapshot_root is None:
        return CheckpointGeneratedMapping(
            output_path=output_path, snapshot_path=None
        ), []
    relative = snapshot_root.relative_to(root).as_posix()
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
    return CheckpointGeneratedMapping(
        output_path=output_path, snapshot_path=relative
    ), files


def validate_generated_mappings(
    root: Path, mappings: list[CheckpointGeneratedMapping], files: list[CheckpointFile]
) -> set[str]:
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
