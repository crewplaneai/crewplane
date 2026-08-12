from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from crewplane.core.execution_state import ArtifactDescriptor
from crewplane.core.file_hashing import file_size_and_sha256

from ..atomic import atomic_write_bytes


@dataclass(frozen=True)
class VerifiedCopyLabels:
    source_artifact: str
    hydrated_artifact: str
    node_id: str

    def changed_message(self, artifact: str, field: str) -> str:
        return f"{artifact} {field} changed for node '{self.node_id}'."


def copy_verified_artifact(
    source_path: Path,
    target_path: Path,
    descriptor: ArtifactDescriptor,
    labels: VerifiedCopyLabels,
) -> ArtifactDescriptor:
    """Copy one descriptor-backed artifact and verify both sides in order."""

    payload = source_path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != descriptor.sha256:
        raise ValueError(labels.changed_message(labels.source_artifact, "hash"))
    if len(payload) != descriptor.size_bytes:
        raise ValueError(labels.changed_message(labels.source_artifact, "size"))
    atomic_write_bytes(target_path, payload)
    target_size, target_sha256 = file_size_and_sha256(target_path)
    if target_size != descriptor.size_bytes:
        raise ValueError(labels.changed_message(labels.hydrated_artifact, "size"))
    if target_sha256 != descriptor.sha256:
        raise ValueError(labels.changed_message(labels.hydrated_artifact, "hash"))
    return ArtifactDescriptor(
        kind=descriptor.kind,
        relative_path=descriptor.relative_path,
        size_bytes=target_size,
        sha256=descriptor.sha256,
    )
