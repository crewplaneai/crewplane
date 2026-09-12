from __future__ import annotations

from pathlib import Path

from crewplane.architecture.ports import ArtifactStorePort
from crewplane.architecture.safe_files import contained_regular_file
from crewplane.core.execution_state import ArtifactDescriptor

from ..generated_files.paths import generated_file_path_belongs_to_node
from .verified_copy import VerifiedCopyLabels, copy_verified_artifact


def copy_generated_file_descriptors(
    source_results_dir: Path,
    output: ArtifactStorePort,
    descriptors: list[ArtifactDescriptor],
    node_id: str,
) -> list[ArtifactDescriptor]:
    return [
        copy_generated_file_descriptor(
            source_results_dir,
            output,
            descriptor,
            node_id,
        )
        for descriptor in descriptors
    ]


def copy_generated_file_descriptor(
    source_results_dir: Path,
    output: ArtifactStorePort,
    descriptor: ArtifactDescriptor,
    node_id: str,
) -> ArtifactDescriptor:
    if descriptor.kind != "generated_file" or not generated_file_path_belongs_to_node(
        descriptor.relative_path, node_id
    ):
        raise ValueError(
            f"Generated-file descriptor for node '{node_id}' is not reusable."
        )
    source_path = contained_regular_file(source_results_dir, descriptor.relative_path)
    if source_path is None:
        raise ValueError(
            f"Generated file artifact for node '{node_id}' is not reusable."
        )
    target_path = output.results_dir / descriptor.relative_path
    return copy_verified_artifact(
        source_path,
        target_path,
        descriptor,
        VerifiedCopyLabels(
            source_artifact="Generated file artifact",
            hydrated_artifact="Hydrated generated file artifact",
            node_id=node_id,
        ),
    )
