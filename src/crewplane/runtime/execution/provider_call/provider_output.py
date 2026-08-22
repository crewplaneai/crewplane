from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path
from typing import Protocol

from crewplane.architecture.safe_files import replace_contained_file

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
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
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
    expected_signature: tuple[int, str] | None = None,
) -> tuple[int, str]:
    """Atomically publish trusted invocation bytes to an unoccupied output path."""

    bound_signature = expected_signature or bind_invocation_output(
        invocation_output_file
    )
    with publications.transaction():
        if invocation_output_file != output_file:
            staged_output = _stage_verified_invocation_output(
                invocation_output_file,
                output_file.parent,
                bound_signature,
            )
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
            finally:
                staged_output.unlink(missing_ok=True)
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


def bind_invocation_output(path: Path) -> tuple[int, str]:
    """Bind one stable single-link output file to its exact byte signature."""

    descriptor, initial_stat = _open_invocation_output(path)
    try:
        size_bytes, sha256 = _hash_descriptor(descriptor)
        _ensure_open_output_unchanged(path, descriptor, initial_stat, size_bytes)
    finally:
        os.close(descriptor)
    return size_bytes, sha256


def read_bound_invocation_output(
    path: Path,
    expected_signature: tuple[int, str],
) -> str:
    """Read exact UTF-8 output bytes only when they match a prior binding."""

    payload = _read_bound_invocation_payload(path, expected_signature)
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(
            f"Invocation output is not valid UTF-8: {path.as_posix()}"
        ) from exc


def _read_bound_invocation_payload(
    path: Path,
    expected_signature: tuple[int, str],
) -> bytes:
    descriptor, initial_stat = _open_invocation_output(path)
    try:
        payload, actual_signature = _read_descriptor(descriptor)
        _ensure_open_output_unchanged(
            path,
            descriptor,
            initial_stat,
            actual_signature[0],
        )
    finally:
        os.close(descriptor)
    if actual_signature != expected_signature:
        raise RuntimeError(
            f"Invocation output does not match its bound bytes: {path.as_posix()}"
        )
    return payload


def _stage_verified_invocation_output(
    source: Path,
    destination_dir: Path,
    expected_signature: tuple[int, str],
) -> Path:
    initial_destination_stat = _real_directory_stat(destination_dir)
    temporary_path, actual_signature = _copy_stable_invocation_output(
        source,
        destination_dir,
    )
    try:
        _validate_staged_invocation_output(
            source,
            destination_dir,
            initial_destination_stat,
            expected_signature,
            actual_signature,
        )
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path


def _copy_stable_invocation_output(
    source: Path,
    destination_dir: Path,
) -> tuple[Path, tuple[int, str]]:
    temporary_path: Path | None = None
    try:
        descriptor, initial_stat = _open_invocation_output(source)
        try:
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
                source,
                descriptor,
                initial_stat,
                actual_signature[0],
            )
        finally:
            os.close(descriptor)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path, actual_signature


def _validate_staged_invocation_output(
    source: Path,
    destination_dir: Path,
    initial_destination_stat: os.stat_result,
    expected_signature: tuple[int, str],
    actual_signature: tuple[int, str],
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


def _open_invocation_output(path: Path) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError(
            f"Invocation output is unavailable or unsafe: {path.as_posix()}"
        ) from exc
    file_stat = os.fstat(descriptor)
    if not _is_single_link_regular_file(file_stat):
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


def _hash_descriptor(descriptor: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    size_bytes = 0
    while chunk := os.read(descriptor, 1024 * 1024):
        size_bytes += len(chunk)
        digest.update(chunk)
    return size_bytes, digest.hexdigest()


def _read_descriptor(descriptor: int) -> tuple[bytes, tuple[int, str]]:
    digest = hashlib.sha256()
    payload = bytearray()
    while chunk := os.read(descriptor, 1024 * 1024):
        payload.extend(chunk)
        digest.update(chunk)
    return bytes(payload), (len(payload), digest.hexdigest())


def _copy_descriptor(descriptor: int, destination: _BinaryWriter) -> tuple[int, str]:
    digest = hashlib.sha256()
    size_bytes = 0
    while chunk := os.read(descriptor, 1024 * 1024):
        destination.write(chunk)
        size_bytes += len(chunk)
        digest.update(chunk)
    return size_bytes, digest.hexdigest()


def _same_file_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        _is_single_link_regular_file(second)
        and first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
        and first.st_ctime_ns == second.st_ctime_ns
    )


def _is_single_link_regular_file(file_stat: os.stat_result) -> bool:
    return stat.S_ISREG(file_stat.st_mode) and file_stat.st_nlink == 1


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
