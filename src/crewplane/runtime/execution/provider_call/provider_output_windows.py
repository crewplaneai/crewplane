from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from crewplane.architecture.safe_file_reads import (
    open_regular_file,
)
from crewplane.core.file_hashing import ContentSignature

from .provider_output_common import copy_descriptor, ensure_open_output_unchanged


@contextmanager
def stage_output(
    source: Path, destination_dir: Path
) -> Iterator[tuple[Path, ContentSignature]]:
    from crewplane.architecture.safe_files_windows import temporary_binary_file

    with temporary_binary_file(destination_dir, ".crewplane-publication-") as (
        temporary_path,
        temporary,
    ):
        with open_output(source) as (descriptor, initial_stat):
            actual_signature = copy_descriptor(descriptor, temporary)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary.close()
            ensure_open_output_unchanged(
                source, descriptor, initial_stat, actual_signature[0]
            )
        yield temporary_path, actual_signature


@contextmanager
def open_output(path: Path) -> Iterator[tuple[int, os.stat_result]]:
    with ExitStack() as protection:
        try:
            descriptor = protection.enter_context(open_regular_file(path))
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"Invocation output is unavailable or unsafe: {path}"
            ) from exc
        yield descriptor, os.fstat(descriptor)
