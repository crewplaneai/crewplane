from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Protocol

from crewplane.architecture.safe_file_reads import (
    bounded_file_chunks,
    open_regular_file,
    stable_file_signature,
)
from crewplane.architecture.safe_files import (
    is_single_link_regular_file,
    replace_contained_file,
)
from crewplane.core.file_hashing import ContentSignature

from ..publication_registry import RuntimePublicationRegistry
from .types import ProviderCallRequest, ProviderOutputPolicy

__all__ = [
    "bind_invocation_output",
    "publish_invocation_output",
    "read_bound_invocation_output",
]


class _BinaryWriter(Protocol):
    def write(self, payload: bytes) -> int: ...


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

    with _protected_invocation_output(path) as (descriptor, initial_stat):
        size_bytes, sha256 = _hash_descriptor(descriptor)
        _ensure_open_output_unchanged(path, descriptor, initial_stat, size_bytes)
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
    with _protected_invocation_output(path) as (descriptor, initial_stat):
        payload, actual_signature = _read_descriptor(descriptor)
        _ensure_open_output_unchanged(
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
    initial_destination_stat = _real_directory_stat(destination_dir)
    with _copy_stable_invocation_output(
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


@contextmanager
def _copy_stable_invocation_output(
    source: Path,
    destination_dir: Path,
) -> Iterator[tuple[Path, ContentSignature]]:
    if os.name == "nt":
        with _copy_windows_invocation_output(source, destination_dir) as staged:
            yield staged
        return
    temporary_path: Path | None = None
    try:
        with _protected_invocation_output(source) as (descriptor, initial_stat):
            with tempfile.NamedTemporaryFile(
                dir=destination_dir,
                prefix=".crewplane-publication-",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                actual_signature = _copy_descriptor(descriptor, temporary)
                temporary.flush()
                os.fsync(temporary.fileno())
            _ensure_open_output_unchanged(
                source, descriptor, initial_stat, actual_signature[0]
            )
        yield temporary_path, actual_signature
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@contextmanager
def _copy_windows_invocation_output(
    source: Path, destination_dir: Path
) -> Iterator[tuple[Path, ContentSignature]]:
    from crewplane.architecture.safe_files_windows import temporary_binary_file

    with temporary_binary_file(destination_dir, ".crewplane-publication-") as (
        temporary_path,
        temporary,
    ):
        with _protected_invocation_output(source) as (descriptor, initial_stat):
            actual_signature = _copy_descriptor(descriptor, temporary)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary.close()
            _ensure_open_output_unchanged(
                source, descriptor, initial_stat, actual_signature[0]
            )
        yield temporary_path, actual_signature


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
    if _same_directory_identity(
        initial_destination_stat,
        _real_directory_stat(destination_dir),
    ):
        return
    raise RuntimeError(
        "Invocation output destination changed during publication: "
        f"{destination_dir.as_posix()}"
    )


@contextmanager
def _protected_invocation_output(path: Path) -> Iterator[tuple[int, os.stat_result]]:
    if os.name == "nt":
        with ExitStack() as protection:
            try:
                descriptor = protection.enter_context(open_regular_file(path))
            except (OSError, ValueError) as exc:
                raise RuntimeError(
                    f"Invocation output is unavailable or unsafe: {path}"
                ) from exc
            yield descriptor, os.fstat(descriptor)
        return
    descriptor, metadata = _open_invocation_output(path)
    try:
        yield descriptor, metadata
    except ValueError as exc:
        raise RuntimeError(
            f"Invocation output changed while being read: {path}"
        ) from exc
    finally:
        os.close(descriptor)


def _open_invocation_output(path: Path) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError(
            f"Invocation output is unavailable or unsafe: {path.as_posix()}"
        ) from exc
    file_stat = os.fstat(descriptor)
    if not is_single_link_regular_file(file_stat):
        os.close(descriptor)
        raise RuntimeError(
            f"Invocation output must be a single-link regular file: {path.as_posix()}"
        )
    return descriptor, file_stat


def _ensure_open_output_unchanged(
    path: Path,
    descriptor: int,
    initial_stat: os.stat_result,
    bytes_read: int,
) -> None:
    final_stat = os.fstat(descriptor)
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise RuntimeError(
            f"Invocation output changed while being read: {path.as_posix()}"
        ) from exc
    if (
        not _same_file_identity(initial_stat, final_stat)
        or not _same_file_identity(final_stat, path_stat)
        or bytes_read != final_stat.st_size
    ):
        raise RuntimeError(
            f"Invocation output changed while being read: {path.as_posix()}"
        )


def _hash_descriptor(descriptor: int) -> ContentSignature:
    digest = hashlib.sha256()
    size_bytes = 0
    for chunk in bounded_file_chunks(descriptor):
        size_bytes += len(chunk)
        digest.update(chunk)
    return size_bytes, digest.hexdigest()


def _read_descriptor(descriptor: int) -> tuple[bytes, ContentSignature]:
    digest = hashlib.sha256()
    payload = bytearray()
    for chunk in bounded_file_chunks(descriptor):
        payload.extend(chunk)
        digest.update(chunk)
    return bytes(payload), (len(payload), digest.hexdigest())


def _copy_descriptor(descriptor: int, destination: _BinaryWriter) -> ContentSignature:
    digest = hashlib.sha256()
    size_bytes = 0
    for chunk in bounded_file_chunks(descriptor):
        destination.write(chunk)
        size_bytes += len(chunk)
        digest.update(chunk)
    return size_bytes, digest.hexdigest()


def _same_file_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return is_single_link_regular_file(second) and stable_file_signature(
        first
    ) == stable_file_signature(second)


def _same_directory_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(second.st_mode)
        and not stat.S_ISLNK(second.st_mode)
        and first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
    )


def _real_directory_stat(path: Path) -> os.stat_result:
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise RuntimeError(
            f"Invocation output destination is unavailable: {path.as_posix()}"
        ) from exc
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
        raise RuntimeError(
            f"Invocation output destination must be a real directory: {path.as_posix()}"
        )
    return path_stat
