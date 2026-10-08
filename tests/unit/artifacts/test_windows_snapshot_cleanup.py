"""Exercise snapshot cleanup policy with local I/O and protection doubles."""

import os
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
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


@pytest.mark.parametrize(
    "failure", ["success", "copy", "mutation", "destination-exit", "source-exit"]
)
def test_snapshot_copy_preserves_result_and_resource_cleanup_order(
    tmp_path, monkeypatch, candidate, failure
):
    target = tmp_path / "snapshot" / candidate.relative_path
    target.parent.mkdir()
    events = []
    descriptors = []

    @contextmanager
    def protect_parent(path):
        handle = LocalHandle(path)
        try:
            yield handle
        finally:
            handle.close()
            events.append("parent closed")

    @contextmanager
    def open_source(path):
        try:
            with path.open("rb") as stream:
                descriptors.append(stream.fileno())
                yield stream.fileno()
        finally:
            events.append("source closed")
        if failure == "source-exit":
            raise ValueError("source exit failed")

    @contextmanager
    def open_destination(path):
        try:
            with path.open("wb") as stream:
                descriptors.append(stream.fileno())
                yield stream.fileno()
        finally:
            events.append("destination closed")
        if failure == "destination-exit":
            raise OSError("destination exit failed")

    original_fdopen = os.fdopen

    class DestinationStream:
        def __init__(self, stream):
            self.stream = stream

        def write(self, payload):
            if failure == "copy":
                raise RuntimeError("copy interrupted")
            result = self.stream.write(payload)
            if failure == "mutation":
                source = candidate.source_path
                initial = source.stat()
                source.write_bytes(b"x" * candidate.size_bytes)
                os.utime(source, ns=(initial.st_atime_ns, initial.st_mtime_ns + 1))
            return result

    @contextmanager
    def fdopen(descriptor, mode, closefd=True):
        with original_fdopen(descriptor, mode, closefd=closefd) as stream:
            yield DestinationStream(stream) if mode == "wb" else stream

    original_delete = safe_files_windows.delete_matching_entry

    def delete(parent, name, identity):
        assert len(descriptors) == 2
        for descriptor in descriptors:
            with pytest.raises(OSError):
                os.fstat(descriptor)
        events.append("destination deleted")
        original_delete(parent, name, identity)

    monkeypatch.setattr(safe_files_windows, "protected_directory", protect_parent)
    monkeypatch.setattr(safe_files_windows, "open_regular_file", open_source)
    monkeypatch.setattr(safe_files_windows, "open_writable_file", open_destination)
    monkeypatch.setattr(safe_files_windows, "delete_matching_entry", delete)
    monkeypatch.setattr(snapshot_io.os, "fdopen", fdopen)

    if failure == "success":
        result = snapshot_io.copy_generated_file_snapshot_candidate(
            candidate, target, tmp_path
        )
        assert result == (len(b"source bytes"), sha256(b"source bytes").hexdigest())
        assert target.read_bytes() == b"source bytes"
    else:
        error_type, message = {
            "copy": (RuntimeError, "copy interrupted"),
            "mutation": (
                RuntimeError,
                "Generated-file snapshot source changed while copying: source.txt",
            ),
            "destination-exit": (OSError, "destination exit failed"),
            "source-exit": (ValueError, "source exit failed"),
        }[failure]
        with pytest.raises(error_type) as error:
            snapshot_io.copy_generated_file_snapshot_candidate(
                candidate, target, tmp_path
            )
        assert str(error.value) == message
        assert not target.exists()

    assert events == [
        "destination closed",
        "source closed",
        *([] if failure == "success" else ["destination deleted"]),
        "parent closed",
    ]
    if failure != "mutation":
        assert candidate.source_path.read_bytes() == b"source bytes"


@pytest.mark.parametrize("failure", ["open-os", "open-value", "metadata"])
def test_source_rejection_preserves_existing_destination_and_exception_cause(
    tmp_path, monkeypatch, candidate, failure
):
    target = tmp_path / "snapshot" / candidate.relative_path
    target.parent.mkdir()
    target.write_bytes(b"existing destination")
    cause = (
        OSError("source unavailable")
        if failure == "open-os"
        else ValueError("unsafe source")
    )
    events = []

    @contextmanager
    def protect_parent(path):
        handle = LocalHandle(path)
        try:
            yield handle
        finally:
            handle.close()
            events.append("parent closed")

    @contextmanager
    def open_source(path):
        if failure != "metadata":
            raise cause
        try:
            with path.open("rb") as stream:
                yield stream.fileno()
        finally:
            events.append("source closed")

    if failure == "metadata":
        candidate = replace(candidate, size_bytes=candidate.size_bytes + 1)
    monkeypatch.setattr(safe_files_windows, "protected_directory", protect_parent)
    monkeypatch.setattr(safe_files_windows, "open_regular_file", open_source)

    with pytest.raises(RuntimeError) as error:
        snapshot_io.copy_generated_file_snapshot_candidate(candidate, target, tmp_path)

    assert str(error.value) == (
        "Generated-file snapshot source changed before copying: source.txt"
    )
    assert error.value.__cause__ is (None if failure == "metadata" else cause)
    assert target.read_bytes() == b"existing destination"
    assert events == (["source closed"] if failure == "metadata" else []) + [
        "parent closed"
    ]
