from __future__ import annotations

import io
import os
from collections.abc import Buffer
from functools import partial
from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.runtime.execution.provider_call.provider_output import (
    bind_invocation_output,
    publish_invocation_output,
    read_bound_invocation_output,
)
from crewplane.runtime.execution.publication_registry import RuntimePublicationRegistry


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink", "hardlink"])
def test_binding_rejects_unavailable_or_shared_output(
    tmp_path: Path, kind: str
) -> None:
    source = tmp_path / "output"
    if kind == "directory":
        source.mkdir()
    elif kind in {"symlink", "hardlink"}:
        target = tmp_path / "target"
        target.write_bytes(b"keep")
        if kind == "symlink":
            source.symlink_to(target)
        else:
            source.hardlink_to(target)
    with pytest.raises(
        RuntimeError, match="unavailable or unsafe|single-link regular file"
    ):
        bind_invocation_output(source)


def test_bound_output_rejects_invalid_utf8(tmp_path: Path) -> None:
    source = tmp_path / "output"
    source.write_bytes(b"\xff")
    signature = bind_invocation_output(source)
    with pytest.raises(RuntimeError, match="not valid UTF-8"):
        read_bound_invocation_output(source, signature)


@pytest.mark.parametrize("action", ["read", "publish", "publish-in-place"])
def test_bound_output_rejects_later_content_changes(
    tmp_path: Path, action: str
) -> None:
    source = tmp_path / "output"
    source.write_bytes(b"original")
    signature = bind_invocation_output(source)
    source.write_bytes(b"modified")
    destination = source if action == "publish-in-place" else tmp_path / "published"
    registry = RuntimePublicationRegistry()
    try:
        operation = (
            partial(read_bound_invocation_output, source, signature)
            if action == "read"
            else partial(
                publish_invocation_output, source, destination, registry, signature
            )
        )
        with pytest.raises(RuntimeError, match="bound bytes|bytes were bound"):
            operation()
        assert registry.snapshot() == ({}, 0)
        assert not list(tmp_path.glob(".crewplane-publication-*"))
        if destination != source:
            assert not destination.exists()
    finally:
        registry.close()


@pytest.mark.parametrize("change", ["unlink", "replace", "append"])
def test_binding_detects_output_changed_during_read(
    tmp_path: Path, change: str
) -> None:
    source = tmp_path / "output"
    source.write_bytes(b"original")
    real_read = os.read
    changed = False

    def read_and_change(descriptor: int, size: int) -> bytes:
        nonlocal changed
        data = real_read(descriptor, size)
        if data and not changed:
            changed = True
            if change == "append":
                with source.open("ab") as stream:
                    stream.write(b"extra")
            else:
                source.unlink()
                if change == "replace":
                    source.write_bytes(b"replacement")
        return data

    with (
        patch.object(os, "read", new=read_and_change),
        pytest.raises(RuntimeError, match="changed while being read"),
    ):
        bind_invocation_output(source)
    assert changed


@pytest.mark.parametrize("kind", ["missing", "file", "symlink"])
def test_publication_rejects_unsafe_destination_directories(
    tmp_path: Path, kind: str
) -> None:
    source = tmp_path / "output"
    source.write_bytes(b"trusted")
    destination_dir = tmp_path / "destination"
    if kind == "file":
        destination_dir.write_bytes(b"keep")
    elif kind == "symlink":
        destination_dir.symlink_to(tmp_path, target_is_directory=True)
    registry = RuntimePublicationRegistry()
    try:
        with pytest.raises(
            RuntimeError, match="destination is unavailable|must be a real directory"
        ):
            publish_invocation_output(source, destination_dir / "published", registry)
        assert registry.snapshot() == ({}, 0)
        assert source.read_bytes() == b"trusted"
    finally:
        registry.close()


def test_publication_cleans_staged_copy_after_disk_failure(tmp_path: Path) -> None:
    source = tmp_path / "output"
    source.write_bytes(b"trusted")
    registry = RuntimePublicationRegistry()
    try:
        with (
            patch.object(os, "fsync", side_effect=OSError("disk failure")),
            pytest.raises(OSError, match="disk failure"),
        ):
            publish_invocation_output(source, tmp_path / "published", registry)
        assert registry.snapshot() == ({}, 0)
        assert sorted(path.name for path in tmp_path.iterdir()) == ["output"]
    finally:
        registry.close()


def test_failed_recovery_capture_preserves_previous_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "output"
    source.write_bytes(b"trusted")
    signature = bind_invocation_output(source)
    registry = RuntimePublicationRegistry()
    try:
        registry.publish(source, signature, recovery_source=source)
        source.write_bytes(b"untrusted")
        with pytest.raises(ValueError, match="does not match its signature"):
            registry.capture_recovery_snapshot(source, (1, "incorrect-digest"))
        restored = io.BytesIO()
        assert registry.copy_recovery_payload_to(source, restored)
        assert restored.getvalue() == b"trusted"
        assert registry.snapshot() == ({source: signature}, 1)
    finally:
        registry.close()


def test_recovery_rejects_hardlinks_without_registering_publication(
    tmp_path: Path,
) -> None:
    source = tmp_path / "output"
    source.write_bytes(b"trusted")
    signature = bind_invocation_output(source)
    (tmp_path / "alias").hardlink_to(source)
    registry = RuntimePublicationRegistry()
    try:
        with pytest.raises(RuntimeError, match="single-link regular file"):
            registry.publish(source, signature, recovery_source=source)
        assert registry.snapshot() == ({}, 0)
        assert not registry.copy_recovery_payload_to(source, io.BytesIO())
    finally:
        registry.close()


class PartialWriter(io.BytesIO):
    def write(self, buffer: Buffer) -> int:
        return super().write(memoryview(buffer)[:1])


def test_recovery_rejects_partial_destination_writes(tmp_path: Path) -> None:
    source = tmp_path / "output"
    source.write_bytes(b"trusted")
    registry = RuntimePublicationRegistry()
    try:
        registry.capture_recovery_snapshot(source, bind_invocation_output(source))
        with pytest.raises(OSError, match="destination accepted a partial write"):
            registry.copy_recovery_payload_to(source, PartialWriter())
        restored = io.BytesIO()
        assert registry.copy_recovery_payload_to(source, restored)
        assert restored.getvalue() == b"trusted"
    finally:
        registry.close()


def test_failed_event_publication_does_not_advance_cursor() -> None:
    registry = RuntimePublicationRegistry()
    try:
        with (
            pytest.raises(OSError, match="write failed"),
            registry.event_publication("owner", b"failed\n"),
        ):
            raise OSError("write failed")
        assert registry.event_publication_cursor() == 0
        with registry.event_publication("owner", b"published\n"):
            pass
        assert [entry.line for entry in registry.event_publications_since(0)] == [
            b"published\n"
        ]
        for invalid_cursor in (-1, 2):
            with pytest.raises(ValueError, match="cursor is invalid"):
                registry.event_publications_since(invalid_cursor)
    finally:
        registry.close()
