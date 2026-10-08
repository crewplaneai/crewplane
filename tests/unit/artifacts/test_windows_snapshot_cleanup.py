"""Exercise snapshot cleanup policy with local I/O and protection doubles."""

import os
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from crewplane.architecture import safe_files_windows, windows_file_handles
from crewplane.artifacts.generated_files import snapshot_io
from crewplane.artifacts.generated_files.snapshot_policy import (
    GeneratedFileSnapshotCandidate,
)
from tests.helpers.platforms import symlink_or_skip
from tests.helpers.windows_file_handles import LocalHandle


@pytest.fixture(autouse=True)
def windows_dispatch(monkeypatch):
    monkeypatch.setattr(
        snapshot_io, "os", SimpleNamespace(**(vars(os) | {"name": "nt"}))
    )

    def descriptor_identity(descriptor, path):
        assert path
        metadata = os.fstat(descriptor)
        return metadata.st_dev, metadata.st_ino

    monkeypatch.setattr(
        windows_file_handles, "descriptor_identity", descriptor_identity
    )


@pytest.fixture
def candidate(tmp_path):
    source = tmp_path / "source.txt"
    source.write_bytes(b"source bytes")
    metadata = source.stat()
    return GeneratedFileSnapshotCandidate(
        source_path=source,
        relative_path=source.relative_to(tmp_path),
        relative_label=source.name,
        size_bytes=metadata.st_size,
        source_device=metadata.st_dev,
        source_inode=metadata.st_ino,
        changed=True,
        discovery_source="workspace_change_baseline",
        explicit=False,
    )


def test_rejected_snapshot_directory_leaves_existing_file_untouched(
    tmp_path, monkeypatch, candidate
):
    target = tmp_path / "unprotected" / candidate.relative_path
    target.parent.mkdir()
    target.write_bytes(b"existing bytes")
    monkeypatch.setattr(
        safe_files_windows,
        "protected_directory",
        Mock(side_effect=ValueError("unsafe directory")),
    )

    with pytest.raises(ValueError, match="unsafe directory"):
        snapshot_io.copy_generated_file_snapshot_candidate(candidate, target, tmp_path)

    assert target.read_bytes() == b"existing bytes"
    assert candidate.source_path.read_bytes() == b"source bytes"


def test_failed_snapshot_copy_cleans_up_before_directory_protection_ends(
    tmp_path, monkeypatch, candidate
):
    target = tmp_path / "snapshot" / candidate.relative_path
    target.parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    external_file = outside / candidate.relative_path
    external_file.write_bytes(b"external bytes")
    retired_directory = tmp_path / "retired-snapshot"

    @contextmanager
    def replace_after_protection(path):
        handle = LocalHandle(path)
        try:
            yield handle
        finally:
            handle.close()
            path.rename(retired_directory)
            symlink_or_skip(path, outside, target_is_directory=True)

    @contextmanager
    def open_source(path):
        with path.open("rb") as source:
            yield source.fileno()

    @contextmanager
    def interrupted_target(path):
        with path.open("wb") as target:
            yield target.fileno()
        raise OSError("copy interrupted")

    monkeypatch.setattr(
        safe_files_windows, "protected_directory", replace_after_protection
    )
    monkeypatch.setattr(safe_files_windows, "open_regular_file", open_source)
    monkeypatch.setattr(safe_files_windows, "open_writable_file", interrupted_target)

    with pytest.raises(OSError, match="copy interrupted"):
        snapshot_io.copy_generated_file_snapshot_candidate(candidate, target, tmp_path)

    assert external_file.read_bytes() == b"external bytes"
    assert not (retired_directory / candidate.relative_path).exists()
    assert candidate.source_path.read_bytes() == b"source bytes"
