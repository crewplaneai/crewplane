"""Check ownership and rollback policy with local handles; not NTFS certification."""

import os
from types import SimpleNamespace

import pytest

from crewplane.architecture import safe_files_windows as operations
from tests.helpers.windows_file_handles import LocalHandle


@pytest.fixture
def handles(monkeypatch):
    opened = []
    calls = []

    def open_handle(path, access=0x80, share=1):
        path.lstat()
        handle = LocalHandle(path, calls)
        opened.append(handle)
        calls.append(("open", path, access, share))
        return handle

    monkeypatch.setattr(operations, "open_handle", open_handle)
    monkeypatch.setattr(
        operations, "os", SimpleNamespace(**(vars(os) | {"O_BINARY": 0}))
    )
    return opened, calls


def test_contained_publication_orders_link_validation_and_owned_deletion(
    tmp_path, handles
):
    opened, calls = handles
    source = tmp_path / "source"
    source.write_bytes(b"exact\r\n\x1a")
    target = operations.replace_contained_file(tmp_path, ("target",), source)
    assert target.read_bytes() == b"exact\r\n\x1a"
    assert target.stat().st_nlink == 1
    assert not source.exists()
    assert ("delete", source) in calls
    assert all(handle.closed for handle in opened)
    assert all(call[3] & 4 == 0 for call in calls if call[0] == "open")


def test_existing_destination_and_hardlinked_source_never_clobber(tmp_path, handles):
    opened, calls = handles
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"ours")
    target.write_bytes(b"other")
    with pytest.raises(FileExistsError):
        operations.replace_contained_file(tmp_path, ("target",), source)
    assert target.read_bytes() == b"other"
    assert not any(call[0] == "delete" for call in calls)
    os.link(source, tmp_path / "alias")
    with pytest.raises(ValueError):
        operations.replace_contained_file(tmp_path, ("new",), source)
    assert all(handle.closed for handle in opened)


def test_failed_source_deletion_rolls_back_only_own_destination(
    tmp_path, handles, monkeypatch
):
    opened, calls = handles
    assert not opened and not calls
    original = LocalHandle.open_child
    source = tmp_path / "source"
    source.write_bytes(b"ours")

    def fail_delete(parent, name, access=0x80, share=1, disposition=1, directory=None):
        path = parent.path / name
        if path == source and access & 0x10000:
            raise PermissionError("sharing violation")
        return original(parent, name, access, share, disposition, directory)

    monkeypatch.setattr(LocalHandle, "open_child", fail_delete)
    with pytest.raises(PermissionError):
        operations.replace_contained_file(tmp_path, ("target",), source)
    assert not (tmp_path / "target").exists()
    assert source.stat().st_nlink == 1
    assert all(handle.closed for handle in opened)


def test_contained_directory_creation_and_binary_append(tmp_path, handles):
    opened, calls = handles
    assert operations.contained_directory(tmp_path, ("new",)) is None
    directory = operations.contained_directory(tmp_path, ("new",), create=True)
    assert directory == tmp_path / "new"
    target = directory / "bytes"
    assert operations.contained_regular_file(directory, ("bytes",)) is None
    with operations.open_writable_file(target) as descriptor:
        os.write(descriptor, b"first\r\n\x1a")
    with operations.open_writable_file(target, append=True) as descriptor:
        os.write(descriptor, b"second")
    with operations.open_regular_file(target) as descriptor:
        assert os.read(descriptor, 100) == b"first\r\n\x1asecond"
    assert operations.contained_regular_file(directory, ("bytes",)) == target
    assert operations.ensure_regular_file(target) == target
    assert all(handle.closed for handle in opened)
    assert calls


def test_long_path_failures_retain_actionable_note(tmp_path, handles, monkeypatch):
    opened, calls = handles
    error = OSError("too long")
    error.winerror = 206

    def fail(path, access=0x80, share=1):
        assert path and access and share
        raise error

    monkeypatch.setattr(operations, "open_handle", fail)
    with pytest.raises(OSError) as result:
        operations.contained_directory(tmp_path, ("long",))
    assert "long-path" in " ".join(result.value.__notes__)
    assert not opened and not calls


def test_local_handle_surrogate_creates_files_without_directory_descriptors(
    tmp_path, monkeypatch
):
    from tests.helpers import windows_file_handles

    monkeypatch.setattr(
        windows_file_handles, "os", SimpleNamespace(**(vars(os) | {"name": "nt"}))
    )
    parent = LocalHandle(tmp_path)
    child = parent.open_child(
        "created", access=0xC0000000, disposition=2, directory=False
    )
    try:
        assert child.validate(directory=False).identity == (
            (tmp_path / "created").stat().st_dev,
            (tmp_path / "created").stat().st_ino,
        )
        descriptor = child.into_descriptor(os.O_RDWR)
        try:
            os.write(descriptor, b"exact\r\n\x1a")
        finally:
            os.close(descriptor)
        assert (tmp_path / "created").read_bytes() == b"exact\r\n\x1a"
    finally:
        child.close()
        parent.close()
