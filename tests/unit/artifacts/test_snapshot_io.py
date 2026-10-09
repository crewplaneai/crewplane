"""Characterize POSIX snapshot validation precedence and descriptor ownership."""

import errno
import os
from dataclasses import replace
from pathlib import Path

import pytest

from crewplane.artifacts.generated_files import io as generated_io
from crewplane.artifacts.generated_files import snapshot_io
from crewplane.artifacts.generated_files.snapshot_policy import (
    GeneratedFileSnapshotCandidate,
)
from tests.helpers.platforms import requires_posix


@requires_posix
@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("directory", "is not a regular file"),
        ("hardlink", "has multiple hard links"),
        ("identity", "changed identity before copying"),
        ("size", "changed size before copying"),
    ],
)
def test_source_validation_preserves_error_precedence_and_closes_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    message: str,
) -> None:
    source = tmp_path / "source.txt"
    source.write_bytes(b"source bytes")
    metadata = source.stat()
    candidate = GeneratedFileSnapshotCandidate(
        source,
        Path(source.name),
        source.name,
        metadata.st_size + 1,
        metadata.st_dev,
        metadata.st_ino,
        None,
        "provider_claim",
        False,
    )
    if change != "size":
        candidate = replace(candidate, source_device=metadata.st_dev + 1)
    if change == "directory":
        source.unlink()
        source.mkdir()
    elif change == "hardlink":
        try:
            os.link(source, tmp_path / "peer.txt")
        except OSError as exc:
            pytest.skip(f"hard links are unavailable: {exc}")
    target = tmp_path / "snapshot.txt"
    target.write_bytes(b"existing destination")
    original_open = os.open
    descriptors: list[int] = []

    def track_open(path: str | Path, flags: int, dir_fd: int | None = None) -> int:
        descriptor = original_open(path, flags, dir_fd=dir_fd)
        if path == source.name:
            descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(snapshot_io.os, "open", track_open)

    with pytest.raises(RuntimeError) as error:
        generated_io.generated_file_operations().copy_snapshot(
            candidate, target, tmp_path
        )

    assert str(error.value) == f"Generated-file snapshot source {message}: source.txt"
    assert not target.exists()
    assert len(descriptors) == 1
    with pytest.raises(OSError) as closed:
        os.fstat(descriptors[0])
    assert closed.value.errno == errno.EBADF
