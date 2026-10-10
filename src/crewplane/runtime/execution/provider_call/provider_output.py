from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from crewplane.architecture.safe_files import (
    is_single_link_regular_file,
    replace_contained_file,
)
from crewplane.core.file_hashing import ContentSignature

from ..publication_registry import RuntimePublicationRegistry
from . import provider_output_io
from .provider_output_common import (
    ensure_open_output_unchanged,
    hash_descriptor,
    read_descriptor,
    real_directory_stat,
    same_directory_identity,
)
from .types import ProviderCallRequest, ProviderOutputPolicy

__all__ = [
    "bind_invocation_output",
    "publish_invocation_output",
    "read_bound_invocation_output",
]


def provider_output_file(request: ProviderCallRequest) -> Path:
    return request.invocation_output_file or request.output_file


def validate_provider_output_file(request: ProviderCallRequest) -> None:
    provider_output = provider_output_file(request)
    if _is_publishable_regular_file(provider_output):
        return
    if request.provider_output_policy == ProviderOutputPolicy.ALLOW_MISSING_OUTPUT:
        return
    raise RuntimeError(
        "Provider invocation completed without the expected output file for "
        f"node '{request.node_id}' task '{request.task_id}': "
        f"{provider_output.as_posix()}"
    )


def _is_publishable_regular_file(path: Path) -> bool:
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RuntimeError(
            f"Provider output could not be inspected safely: {path.as_posix()}"
        ) from exc
    if not is_single_link_regular_file(file_stat):
        raise RuntimeError(
            f"Provider output must be a single-link regular file: {path.as_posix()}"
        )
    return True


def publish_provider_output(request: ProviderCallRequest) -> None:
    provider_output = provider_output_file(request)
    if not _is_publishable_regular_file(provider_output):
        return
    publication_signature = publish_invocation_output(
        provider_output,
        request.output_file,
        request.runtime_context.runtime_publications,
    )
    if request.on_invocation_output_published is not None:
        request.on_invocation_output_published(
            request.output_file,
            publication_signature,
        )


def publish_invocation_output(
    invocation_output_file: Path,
    output_file: Path,
    publications: RuntimePublicationRegistry,
    expected_signature: ContentSignature | None = None,
) -> ContentSignature:
    """Atomically publish trusted invocation bytes to an unoccupied output path."""

    bound_signature = expected_signature or bind_invocation_output(
        invocation_output_file
    )
    with publications.transaction():
        if invocation_output_file != output_file:
            with _stage_verified_invocation_output(
                invocation_output_file,
                output_file.parent,
                bound_signature,
            ) as staged_output:
                try:
                    replace_contained_file(
                        output_file.parent,
                        output_file.name,
                        staged_output,
                    )
                except (OSError, ValueError) as exc:
                    raise RuntimeError(
                        "Refusing to publish an invocation output through an unsafe "
                        f"destination: {output_file.as_posix()}"
                    ) from exc
        publication_signature = bind_invocation_output(output_file)
        if publication_signature != bound_signature:
            raise RuntimeError(
                "Published invocation output does not match its bound bytes: "
                f"{output_file.as_posix()}"
            )
        publications.publish(
            output_file,
            bound_signature,
            recovery_source=output_file,
        )
    return bound_signature


def bind_invocation_output(path: Path) -> ContentSignature:
    """Bind one stable single-link output file to its exact byte signature."""

    with provider_output_io.provider_output_operations().open_output(path) as (
        descriptor,
        initial_stat,
    ):
        size_bytes, sha256 = hash_descriptor(descriptor)
        ensure_open_output_unchanged(path, descriptor, initial_stat, size_bytes)
    return size_bytes, sha256


def read_bound_invocation_output(
    path: Path,
    expected_signature: ContentSignature,
) -> str:
    """Verify bound output bytes before rendering text with UTF-8 replacement."""

    payload = _read_bound_invocation_payload(path, expected_signature)
    return payload.decode("utf-8", errors="replace")


def _read_bound_invocation_payload(
    path: Path,
    expected_signature: ContentSignature,
) -> bytes:
    with provider_output_io.provider_output_operations().open_output(path) as (
        descriptor,
        initial_stat,
    ):
        payload, actual_signature = read_descriptor(descriptor)
        ensure_open_output_unchanged(
            path,
            descriptor,
            initial_stat,
            actual_signature[0],
        )
    if actual_signature != expected_signature:
        raise RuntimeError(
            f"Invocation output does not match its bound bytes: {path.as_posix()}"
        )
    return payload


@contextmanager
def _stage_verified_invocation_output(
    source: Path,
    destination_dir: Path,
    expected_signature: ContentSignature,
) -> Iterator[Path]:
    initial_destination_stat = real_directory_stat(destination_dir)
    with provider_output_io.provider_output_operations().stage_output(
        source,
        destination_dir,
    ) as (temporary_path, actual_signature):
        _validate_staged_invocation_output(
            source,
            destination_dir,
            initial_destination_stat,
            expected_signature,
            actual_signature,
        )
        yield temporary_path


def _validate_staged_invocation_output(
    source: Path,
    destination_dir: Path,
    initial_destination_stat: os.stat_result,
    expected_signature: ContentSignature,
    actual_signature: ContentSignature,
) -> None:
    if actual_signature != expected_signature:
        raise RuntimeError(
            f"Invocation output changed after its bytes were bound: {source.as_posix()}"
        )
    if same_directory_identity(
        initial_destination_stat,
        real_directory_stat(destination_dir),
    ):
        return
    raise RuntimeError(
        "Invocation output destination changed during publication: "
        f"{destination_dir.as_posix()}"
    )
