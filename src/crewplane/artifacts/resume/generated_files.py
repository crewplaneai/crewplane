from __future__ import annotations

import hashlib
from pathlib import Path

from crewplane.architecture.ports import ArtifactStorePort
from crewplane.core.execution_state import ArtifactDescriptor
from crewplane.core.file_hashing import file_size_and_sha256

from ..atomic import atomic_write_bytes
from ..naming import build_generated_file_result_dir_name
from ..safe_files import contained_regular_file


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
    payload = source_path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != descriptor.sha256:
        raise ValueError(f"Generated file artifact hash changed for node '{node_id}'.")
    if len(payload) != descriptor.size_bytes:
        raise ValueError(f"Generated file artifact size changed for node '{node_id}'.")
    target_path = output.results_dir / descriptor.relative_path
    atomic_write_bytes(target_path, payload)
    target_size, target_sha256 = file_size_and_sha256(target_path)
    if target_size != descriptor.size_bytes:
        raise ValueError(
            f"Hydrated generated file artifact size changed for node '{node_id}'."
        )
    if target_sha256 != descriptor.sha256:
        raise ValueError(
            f"Hydrated generated file artifact hash changed for node '{node_id}'."
        )
    return ArtifactDescriptor(
        kind=descriptor.kind,
        relative_path=descriptor.relative_path,
        size_bytes=target_size,
        sha256=descriptor.sha256,
    )


def generated_file_path_belongs_to_node(relative_path: str, node_id: str) -> bool:
    parts = relative_path.split("/")
    expected_node_dir = build_generated_file_result_dir_name(node_id)
    return (
        len(parts) >= 4
        and parts[0] == "generated-files"
        and parts[1] == expected_node_dir
        and all(part not in {"", ".", ".."} for part in parts)
    )
