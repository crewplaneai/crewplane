from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from crewplane.architecture.safe_files import (
    is_single_link_regular_file,
)
from crewplane.core.file_hashing import ContentSignature

from .provider_output_common import copy_descriptor, ensure_open_output_unchanged


@contextmanager
def stage_output(
    source: Path,
    destination_dir: Path,
) -> Iterator[tuple[Path, ContentSignature]]:
    temporary_path: Path | None = None
    try:
        with open_output(source) as (descriptor, initial_stat):
            with tempfile.NamedTemporaryFile(
                dir=destination_dir,
                prefix=".crewplane-publication-",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                actual_signature = copy_descriptor(descriptor, temporary)
                temporary.flush()
                os.fsync(temporary.fileno())
            ensure_open_output_unchanged(
                source, descriptor, initial_stat, actual_signature[0]
            )
        yield temporary_path, actual_signature
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@contextmanager
def open_output(path: Path) -> Iterator[tuple[int, os.stat_result]]:
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
