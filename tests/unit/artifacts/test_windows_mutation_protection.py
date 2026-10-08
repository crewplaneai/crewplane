"""Check mutation lifetimes with local handles; native cases run separately."""

import os
from contextlib import contextmanager, suppress
from types import SimpleNamespace

import pytest

from crewplane.architecture import safe_files, safe_files_windows, windows_file_handles
from crewplane.artifacts import atomic
from crewplane.artifacts.generated_files import catalog, snapshot_io, snapshot_metadata
from crewplane.artifacts.generated_files.snapshot_policy import (
    GeneratedFileSnapshotCandidate,
)
from crewplane.runtime.execution.provider_call import provider_output
from crewplane.runtime.execution.publication_registry import RuntimePublicationRegistry
from tests.helpers.platforms import symlink_or_skip
from tests.helpers.windows_file_handles import LocalHandle

pytestmark = pytest.mark.usefixtures("windows_dispatch")


@pytest.fixture
def windows_dispatch(monkeypatch):
    windows_os = SimpleNamespace(**(vars(os) | {"name": "nt"}))
    monkeypatch.setattr(provider_output, "os", windows_os)
    monkeypatch.setattr(catalog, "os", windows_os)
    monkeypatch.setattr(safe_files, "os", windows_os)
    monkeypatch.setattr(atomic, "os", windows_os)
    monkeypatch.setattr(snapshot_io, "os", windows_os)

    @contextmanager
    def open_source(path):
        with path.open("rb") as source:
            yield source.fileno()

    def read_bytes(root, relative):
        return (root / relative).read_bytes()

    def descriptor_identity(descriptor, path):
        assert path
        metadata = os.fstat(descriptor)
        return metadata.st_dev, metadata.st_ino

    def open_handle(path, access=0x80, share=1):
        assert access and share
        path.lstat()
        return LocalHandle(path)

    monkeypatch.setattr(
        windows_file_handles, "descriptor_identity", descriptor_identity, raising=False
    )
    monkeypatch.setattr(safe_files_windows, "open_handle", open_handle)
    monkeypatch.setattr(provider_output, "open_regular_file", open_source)
    monkeypatch.setattr(catalog, "read_contained_bytes", read_bytes)


@pytest.mark.parametrize("failure", ["copy", "validation", "publication"])
def test_publication_cleanup_precedes_parent_replacement(
    tmp_path, monkeypatch, failure
):
    source = tmp_path / "source"
    source.write_bytes(b"trusted")
    signature = provider_output.bind_invocation_output(source)
    if failure == "validation":
        signature = signature[0], "0" * 64
    destination = tmp_path / "destination"
    destination.mkdir()
    retired = tmp_path / "retired"
    outside = tmp_path / "outside"
    outside.mkdir()
    external_file = None

    @contextmanager
    def replace_after_protection(path):
        nonlocal external_file
        assert path == destination
        handle = LocalHandle(path)
        try:
            yield handle
        finally:
            handle.close()
            if external_file is None:
                staged = next(path.glob(".crewplane-publication-*"))
                external_file = outside / staged.name
                external_file.write_bytes(b"external bytes")
            path.rename(retired)
            symlink_or_skip(path, outside, target_is_directory=True)

    original_fsync = os.fsync

    def flush(descriptor):
        nonlocal external_file
        staged = next(
            (retired if retired.exists() else destination).glob(
                ".crewplane-publication-*"
            )
        )
        external_file = outside / staged.name
        external_file.write_bytes(b"external bytes")
        if failure == "copy":
            raise OSError("publication interrupted")
        original_fsync(descriptor)

    def fail(*args):
        assert args
        raise OSError("publication interrupted")

    monkeypatch.setattr(
        safe_files_windows, "protected_directory", replace_after_protection
    )
    monkeypatch.setattr(provider_output.os, "fsync", flush)
    if failure == "publication":
        monkeypatch.setattr(provider_output, "replace_contained_file", fail)

    registry = RuntimePublicationRegistry()
    try:
        with pytest.raises((RuntimeError, OSError)):
            provider_output.publish_invocation_output(
                source, destination / "published", registry, signature
            )
        assert external_file is not None
        assert external_file.read_bytes() == b"external bytes"
        assert list(retired.iterdir()) == []
        assert source.read_bytes() == b"trusted"
        assert registry.snapshot() == ({}, 0)
    finally:
        registry.close()


def test_publication_cleanup_preserves_a_competing_temporary_entry(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    source.write_bytes(b"trusted")
    competitor = None

    def replace_and_fail(root, relative, staged):
        nonlocal competitor
        assert root == tmp_path and relative == "published"
        staged.rename(tmp_path / "retired-stage")
        staged.write_bytes(b"competitor")
        competitor = staged
        raise OSError("publication interrupted")

    monkeypatch.setattr(provider_output, "replace_contained_file", replace_and_fail)
    registry = RuntimePublicationRegistry()
    try:
        with pytest.raises(RuntimeError, match="unsafe destination"):
            provider_output.publish_invocation_output(
                source, tmp_path / "published", registry
            )
        assert competitor is not None
        assert competitor.read_bytes() == b"competitor"
        assert (tmp_path / "retired-stage").read_bytes() == b"trusted"
        assert not (tmp_path / "published").exists()
        assert registry.snapshot() == ({}, 0)
    finally:
        registry.close()


@pytest.mark.parametrize("existing", [False, True], ids=["creation", "replacement"])
def test_snapshot_root_mutations_precede_parent_replacement(
    tmp_path, monkeypatch, existing
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = tmp_path / "provider.md"
    output.write_bytes(b"No generated files.\n")
    parent = tmp_path / "snapshots"
    parent.mkdir()
    snapshot = parent / "candidate"
    if existing:
        nested = snapshot / "nested"
        nested.mkdir(parents=True)
        (nested / "stale").write_bytes(b"stale")
    retired = tmp_path / "retired"
    outside = tmp_path / "outside"
    external_snapshot = outside / snapshot.name
    external_snapshot.mkdir(parents=True)
    external_file = external_snapshot / "keep"
    external_file.write_bytes(b"external bytes")

    @contextmanager
    def replace_after_protection(path, create=False):
        if create:
            path.mkdir(parents=True, exist_ok=True)
        handle = LocalHandle(path)
        try:
            yield handle
        finally:
            handle.close()
            if path == parent:
                path.rename(retired)
                symlink_or_skip(path, outside, target_is_directory=True)

    def stop_before_metadata(root, source_root):
        assert root == snapshot and source_root == workspace
        raise OSError("metadata stopped")

    monkeypatch.setattr(
        safe_files_windows, "protected_directory", replace_after_protection
    )
    monkeypatch.setattr(
        snapshot_metadata, "write_source_metadata", stop_before_metadata
    )

    with pytest.raises(OSError, match="metadata stopped"):
        catalog.snapshot_generated_file_workspace(
            output, workspace, snapshot_root=snapshot
        )

    assert external_file.read_bytes() == b"external bytes"
    assert list(external_snapshot.iterdir()) == [external_file]
    assert (retired / snapshot.name).is_dir()
    assert list((retired / snapshot.name).iterdir()) == []


@pytest.mark.skipif(
    os.name == "nt", reason="Native junction tests cover this descriptor surrogate"
)
def test_snapshot_creation_does_not_follow_parent_redirect_after_validation(
    tmp_path, monkeypatch
):
    parent = tmp_path / "snapshots"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    original = LocalHandle.validate
    redirected = False

    def redirect(handle, directory, links=1):
        nonlocal redirected
        result = original(handle, directory, links)
        if handle.path == parent and not redirected:
            redirected = True
            parent.rename(tmp_path / "original-parent")
            symlink_or_skip(parent, outside, target_is_directory=True)
        return result

    monkeypatch.setattr(LocalHandle, "validate", redirect)
    with suppress(OSError, ValueError):
        safe_files_windows.reset_directory(parent / "candidate")
    assert redirected
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(
    os.name == "nt", reason="Native junction tests cover this descriptor surrogate"
)
def test_snapshot_deletion_does_not_follow_redirect_after_validation(
    tmp_path, monkeypatch
):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    external_file = outside / "keep"
    external_file.write_bytes(b"external bytes")
    original = LocalHandle.validate
    redirected = False

    def redirect(handle, directory, links=1):
        nonlocal redirected
        result = original(handle, directory, links)
        if handle.path == snapshot and not redirected:
            redirected = True
            snapshot.rename(tmp_path / "original-snapshot")
            symlink_or_skip(snapshot, outside, target_is_directory=True)
        return result

    monkeypatch.setattr(LocalHandle, "validate", redirect)
    with suppress(OSError, ValueError):
        safe_files_windows.reset_directory(snapshot)
    assert redirected
    assert external_file.read_bytes() == b"external bytes"
    assert list(outside.iterdir()) == [external_file]


@pytest.mark.skipif(
    os.name == "nt", reason="Native junction tests cover this descriptor surrogate"
)
@pytest.mark.parametrize(
    "operation", ["directories", "file", "temporary", "metadata", "publication"]
)
def test_empty_directory_redirect_does_not_receive_generated_bytes(
    tmp_path, monkeypatch, operation
):
    parent = tmp_path / "destination"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    source = tmp_path / "source"
    source.write_bytes(b"trusted")
    original = LocalHandle.validate
    redirected = False

    def redirect(handle, directory, links=1):
        nonlocal redirected
        result = original(handle, directory, links)
        if handle.path == parent and not redirected:
            redirected = True
            parent.rename(tmp_path / "original-parent")
            symlink_or_skip(parent, outside, target_is_directory=True)
        return result

    monkeypatch.setattr(LocalHandle, "validate", redirect)
    registry = RuntimePublicationRegistry()
    try:
        with suppress(OSError, ValueError, RuntimeError):
            if operation == "directories":
                safe_files.ensure_contained_directory(parent, "new/nested")
            elif operation == "file":
                safe_files.ensure_single_link_regular_file(parent / "generated")
            elif operation == "temporary":
                with safe_files_windows.temporary_binary_file(parent, "stage-") as (
                    _,
                    stream,
                ):
                    stream.write(b"ours")
            elif operation == "metadata":
                atomic.atomic_write_bytes(parent / "metadata.json", b"ours")
            else:
                provider_output.publish_invocation_output(
                    source, parent / "published", registry
                )
        assert redirected
        assert list(outside.iterdir()) == []
        assert source.read_bytes() == b"trusted"
    finally:
        registry.close()


@pytest.mark.skipif(
    os.name == "nt", reason="Descriptor surrogate for native handle-relative cleanup"
)
@pytest.mark.parametrize("native_device_matches_stat", [True, False])
def test_snapshot_copy_failure_cleans_owned_entry_through_redirected_parent(
    tmp_path, monkeypatch, native_device_matches_stat
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "generated"
    source.write_bytes(b"trusted")
    metadata = source.stat()
    candidate = GeneratedFileSnapshotCandidate(
        source,
        source.relative_to(workspace),
        "generated",
        metadata.st_size,
        metadata.st_dev,
        metadata.st_ino,
        None,
        "explicit",
        True,
    )
    if not native_device_matches_stat:
        original_information = LocalHandle.information

        def information(handle):
            info = original_information(handle)
            info.identity = info.identity[0] ^ 1, info.identity[1]
            return info

        def descriptor_identity(descriptor, path):
            assert path
            metadata = os.fstat(descriptor)
            return metadata.st_dev ^ 1, metadata.st_ino

        monkeypatch.setattr(LocalHandle, "information", information)
        monkeypatch.setattr(
            windows_file_handles, "descriptor_identity", descriptor_identity
        )
    parent = tmp_path / "snapshot"
    parent.mkdir()
    retired = tmp_path / "original-snapshot"
    outside = tmp_path / "outside"
    outside.mkdir()
    external_file = outside / "generated"
    external_file.write_bytes(b"external bytes")

    original_fdopen = os.fdopen

    class InterruptedStream:
        def __init__(self, stream):
            self.stream = stream

        def write(self, payload):
            assert payload == b"trusted"
            self.stream.write(b"partial")
            self.stream.flush()
            parent.rename(retired)
            symlink_or_skip(parent, outside, target_is_directory=True)
            raise RuntimeError("copy interrupted")

    @contextmanager
    def fdopen(descriptor, mode, closefd=True):
        with original_fdopen(descriptor, mode, closefd=closefd) as stream:
            yield InterruptedStream(stream) if mode == "wb" else stream

    monkeypatch.setattr(snapshot_io.os, "fdopen", fdopen)
    with pytest.raises(RuntimeError, match="copy interrupted"):
        snapshot_io.copy_generated_file_snapshot_candidate(
            candidate, parent / "generated", workspace
        )
    assert external_file.read_bytes() == b"external bytes"
    assert list(outside.iterdir()) == [external_file]
    assert list(retired.iterdir()) == []
    assert source.read_bytes() == b"trusted"
