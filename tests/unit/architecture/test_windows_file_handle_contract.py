import ctypes
import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from crewplane.architecture import windows_file_handles as handles


@pytest.fixture
def native(monkeypatch, tmp_path):
    api = Mock()
    api.CreateFileW.return_value = 42
    api.CloseHandle.return_value = True
    api.GetFileType.return_value = 1
    api.SetFileInformationByHandle.return_value = True
    info = handles.FileInformation()
    info.links = 1
    info.volume = 10
    info.index_low = 12

    def information(handle, pointer):
        assert handle == 42
        ctypes.memmove(pointer, ctypes.byref(info), ctypes.sizeof(info))
        return True

    def final_path(handle, buffer, size, flags):
        assert handle == 42 and flags == 0
        value = str(tmp_path / "source")
        if size:
            buffer.value = value
        return len(value) + (0 if size else 1)

    def long_path(path, buffer, size):
        if size:
            buffer.value = path
        return len(path) + (0 if size else 1)

    api.GetFileInformationByHandle.side_effect = information
    api.GetFinalPathNameByHandleW.side_effect = final_path
    api.GetLongPathNameW.side_effect = long_path
    monkeypatch.setattr(handles, "kernel32", Mock(return_value=api))
    monkeypatch.setattr(
        ctypes, "WinError", lambda code: OSError(code, "native failure"), raising=False
    )
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    return api, info


def test_handle_flags_identity_and_owned_deletion(native, tmp_path):
    api, info = native
    handle = handles.open_handle(tmp_path / "source")
    args = api.CreateFileW.call_args.args
    assert args[2] == handles.FILE_SHARE_READ and args[3] is None
    assert args[5] & handles.FILE_FLAG_OPEN_REPARSE_POINT
    assert handle.validate(False).identity == (10, 12)
    info.index_high = 1
    assert handle.information().identity == (10, (1 << 32) + 12)
    handle.delete()
    assert api.SetFileInformationByHandle.call_args.args[1] == 4
    handle.close()
    handle.close()
    api.CloseHandle.assert_called_once_with(42)


@pytest.mark.parametrize("invalid", ["reparse", "directory", "links", "type", "path"])
def test_handle_rejects_invalid_opened_state(native, tmp_path, invalid):
    api, info = native
    path = tmp_path / "source"
    if invalid == "reparse":
        info.attributes = 0x400
    elif invalid == "directory":
        info.attributes = 0x10
    elif invalid == "links":
        info.links = 2
    elif invalid == "type":
        api.GetFileType.return_value = 2
    else:
        path = tmp_path / "other"
    handle = handles.open_handle(path)
    try:
        with pytest.raises(ValueError):
            handle.validate(False)
    finally:
        handle.close()


@pytest.mark.parametrize(
    "operation",
    [
        "CreateFileW",
        "GetFileInformationByHandle",
        "GetFinalPathNameByHandleW",
        "SetFileInformationByHandle",
        "CloseHandle",
    ],
)
def test_handle_failures_are_explicit(native, tmp_path, operation):
    api, info = native
    assert info.links == 1
    function = getattr(api, operation)
    function.side_effect = None
    function.return_value = (
        ctypes.c_void_p(-1).value if operation == "CreateFileW" else 0
    )
    with pytest.raises(OSError):
        handle = handles.open_handle(tmp_path / "source")
        if operation == "GetFileInformationByHandle":
            handle.information()
        elif operation == "GetFinalPathNameByHandleW":
            handle.final_path()
        elif operation == "SetFileInformationByHandle":
            handle.delete()
        elif operation == "CloseHandle":
            handle.close()


def test_descriptor_transfer_uses_binary_and_transfers_ownership(
    native, tmp_path, monkeypatch
):
    api, info = native
    assert info.links == 1
    transfer = Mock(return_value=99)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(open_osfhandle=transfer))
    monkeypatch.setattr(os, "O_BINARY", 0x8000, raising=False)
    handle = handles.open_handle(tmp_path / "source")
    assert handle.into_descriptor() == 99
    assert transfer.call_args.args == (42, os.O_RDONLY | 0x8000)
    handle.close()
    api.CloseHandle.assert_not_called()


@pytest.mark.parametrize("failed", [False, True])
def test_descriptor_identity_preserves_crt_ownership(
    native, tmp_path, monkeypatch, failed
):
    api, info = native
    assert info.links == 1
    lookup = Mock(return_value=42)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lookup))
    if failed:
        api.GetFileInformationByHandle.side_effect = None
        api.GetFileInformationByHandle.return_value = False
        with pytest.raises(OSError):
            handles.descriptor_identity(99, tmp_path / "source")
    else:
        assert handles.descriptor_identity(99, tmp_path / "source") == (10, 12)
    lookup.assert_called_once_with(99)
    api.CloseHandle.assert_not_called()


def test_lazy_native_binding_and_long_path_diagnostic(monkeypatch, tmp_path):
    handles.kernel32.cache_clear()
    monkeypatch.setattr(handles, "os", SimpleNamespace(name="posix"))
    with pytest.raises(RuntimeError):
        handles.kernel32()
    monkeypatch.setattr(handles, "os", SimpleNamespace(name="nt"))
    api = Mock()
    monkeypatch.setattr(ctypes, "WinDLL", Mock(return_value=api), raising=False)
    monkeypatch.setattr(
        ctypes, "WinError", lambda code: OSError(code, "path too long"), raising=False
    )
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 206, raising=False)
    try:
        assert handles.kernel32() is api
        assert len(api.CreateFileW.argtypes) == 7
        assert "long-path" in " ".join(handles.path_error(tmp_path).__notes__)
    finally:
        handles.kernel32.cache_clear()


@pytest.mark.parametrize(
    ("expected", "resolved"),
    [
        (r"\\?\C:\project\source", r"C:\project\source"),
        (r"\\?\UNC\server\share\source", r"\\server\share\source"),
    ],
)
def test_handle_accepts_equivalent_extended_paths(
    native, monkeypatch, expected, resolved
):
    from pathlib import Path

    _api, info = native
    assert info.links == 1
    monkeypatch.setattr(handles.os.path, "abspath", Mock(return_value=expected))
    monkeypatch.setattr(handles.FileHandle, "final_path", Mock(return_value=resolved))
    handle = handles.FileHandle(42, Path("source"))
    try:
        assert handle.validate(False).identity == (10, 12)
    finally:
        handle.close()


@pytest.mark.parametrize(
    "expected",
    [
        r"C:\Users\RUNNER~1\source",
        r"\\?\C:\Users\RUNNER~1\source",
        r"\\server\share\RUNNER~1\source",
        r"\\?\UNC\server\share\RUNNER~1\source",
    ],
)
def test_handle_accepts_short_path_aliases(native, monkeypatch, tmp_path, expected):
    api, _info = native
    normalized = expected.replace("RUNNER~1", "runneradmin")
    resolved = normalized.replace("\\\\?\\UNC\\", "\\\\").removeprefix("\\\\?\\")

    def long_path(path, buffer, size):
        assert path.startswith("\\\\?\\")
        assert "RUNNER~1" in path
        if size:
            buffer.value = normalized
        return len(normalized) + (0 if size else 1)

    api.GetLongPathNameW.side_effect = long_path
    monkeypatch.setattr(handles.os.path, "abspath", Mock(return_value=expected))
    monkeypatch.setattr(handles.FileHandle, "final_path", Mock(return_value=resolved))
    handle = handles.FileHandle(42, tmp_path / "source")
    try:
        assert handle.validate(False).identity == (10, 12)
        assert api.GetLongPathNameW.call_count == 2
    finally:
        handle.close()


@pytest.mark.parametrize("failure", ["size", "read", "growth"])
def test_short_path_normalization_failures_are_explicit(native, tmp_path, failure):
    api, _info = native
    api.GetLongPathNameW.side_effect = {
        "size": [0],
        "read": [20, 0],
        "growth": [20, 30],
    }[failure]
    handle = handles.FileHandle(42, tmp_path / "alias")
    try:
        with pytest.raises(OSError, match="native failure"):
            handle.validate(False)
    finally:
        handle.close()


@pytest.fixture
def native_relative(native, monkeypatch):
    _kernel, info = native
    assert info.links == 1
    api = Mock()
    api.RtlNtStatusToDosError.return_value = 5
    api.NtSetInformationFile.return_value = 0

    def create(
        value,
        access,
        attributes,
        io,
        allocation,
        flags,
        share,
        disposition,
        options,
        ea,
        size,
    ):
        assert access and io and allocation is None and flags == 0
        assert share and disposition and options and ea is None and size == 0
        attributes = ctypes.cast(
            attributes, ctypes.POINTER(handles.ObjectAttributes)
        ).contents
        text = attributes.name.contents
        api.relative_name = ctypes.string_at(text.buffer, text.length).decode(
            "utf-16-le"
        )
        ctypes.cast(value, ctypes.POINTER(ctypes.c_void_p)).contents.value = 43
        return 0

    api.NtCreateFile.side_effect = create
    monkeypatch.setattr(handles, "ntdll", Mock(return_value=api))
    return api


@pytest.mark.parametrize("directory", [None, False, True])
def test_relative_open_anchors_one_literal_component(
    native_relative, tmp_path, directory
):
    parent = handles.FileHandle(42, tmp_path)
    child = parent.open_child(
        "café 🌍", disposition=handles.FILE_CREATE, directory=directory
    )
    api = native_relative
    args = api.NtCreateFile.call_args.args
    assert args[1] & 0x100000  # SYNCHRONIZE for synchronous NT handles.
    attributes = ctypes.cast(args[2], ctypes.POINTER(handles.ObjectAttributes)).contents
    assert attributes.root == 42
    assert api.relative_name == "café 🌍"
    assert args[6:8] == (handles.FILE_SHARE_READ, handles.FILE_CREATE)
    assert args[8] & 0x200000  # FILE_OPEN_REPARSE_POINT, without following the child.
    if directory is not None:
        assert args[8] & (1 if directory else 0x40)
    assert child.value == 43 and child.path == tmp_path / "café 🌍"
    child.close()


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", "one/two", "one\\two", "ads:stream", "bad\0name", "x" * 32767],
    ids=["empty", "dot", "parent", "slash", "backslash", "stream", "nul", "too-long"],
)
def test_relative_operations_reject_non_component_names(
    native_relative, tmp_path, name
):
    parent = handles.FileHandle(42, tmp_path)
    with pytest.raises(ValueError):
        parent.open_child(name)
    with pytest.raises(ValueError):
        parent.publish_entry(parent, name)
    native_relative.NtCreateFile.assert_not_called()
    native_relative.NtSetInformationFile.assert_not_called()


@pytest.mark.parametrize("rename", [False, True])
def test_entry_publication_uses_parent_handle_and_literal_name(
    native_relative, tmp_path, rename
):
    source = handles.FileHandle(42, tmp_path / "source")
    target = handles.FileHandle(43, tmp_path / "destination")
    source.publish_entry(target, "café 🌍", rename)
    args = native_relative.NtSetInformationFile.call_args.args
    info = handles.EntryInformation.from_buffer(args[2])
    assert args[0] == 42 and args[4] == (10 if rename else 11)
    assert info.root == 43 and bool(info.replace) == rename
    start = handles.EntryInformation.name.offset
    assert args[2].raw[start : start + info.length].decode("utf-16-le") == "café 🌍"


@pytest.mark.parametrize("operation", ["open", "link", "rename"])
def test_relative_operation_failures_preserve_path_diagnostics(
    native_relative, tmp_path, operation
):
    parent = handles.FileHandle(42, tmp_path)
    native_relative.NtCreateFile.side_effect = None
    native_relative.NtCreateFile.return_value = -1
    native_relative.NtSetInformationFile.return_value = -1
    with pytest.raises(OSError) as error:
        if operation == "open":
            parent.open_child("target")
        else:
            parent.publish_entry(parent, "target", operation == "rename")
    assert error.value.filename == str(tmp_path / "target")
    native_relative.RtlNtStatusToDosError.assert_called_once_with(-1)


def test_directory_enumeration_uses_handle_and_continuation(
    native, monkeypatch, tmp_path
):
    api, _info = native
    calls = 0

    def enumerate_entries(value, information_class, buffer, size):
        nonlocal calls
        assert value == 42 and size == 65536
        calls += 1
        if calls == 3:
            return False
        assert information_class == (11 if calls == 1 else 10)
        names = (".", "café 🌍") if calls == 1 else ("last",)
        offset = 0
        for index, name in enumerate(names):
            text = name.encode("utf-16-le")
            info = handles.DirectoryInformation.from_buffer(buffer, offset)
            next_offset = (
                handles.DirectoryInformation.name.offset + len(text) + 7
            ) & ~7
            info.next_offset = next_offset if index + 1 < len(names) else 0
            info.name_length = len(text)
            ctypes.memmove(
                ctypes.addressof(buffer)
                + offset
                + handles.DirectoryInformation.name.offset,
                text,
                len(text),
            )
            offset += next_offset
        return True

    monkeypatch.setattr(ctypes, "get_last_error", lambda: 18)
    api.GetFileInformationByHandleEx.side_effect = enumerate_entries
    assert list(handles.FileHandle(42, tmp_path).entry_names()) == ["café 🌍", "last"]


def test_directory_enumeration_errors_are_explicit(native, tmp_path):
    api, _info = native
    api.GetFileInformationByHandleEx.return_value = False
    with pytest.raises(OSError):
        list(handles.FileHandle(42, tmp_path).entry_names())


def test_nt_binding_is_lazy_and_has_explicit_signatures(monkeypatch):
    handles.ntdll.cache_clear()
    monkeypatch.setattr(handles, "os", SimpleNamespace(name="posix"))
    with pytest.raises(RuntimeError):
        handles.ntdll()
    monkeypatch.setattr(handles, "os", SimpleNamespace(name="nt"))
    api = Mock()
    monkeypatch.setattr(ctypes, "WinDLL", Mock(return_value=api), raising=False)
    try:
        assert handles.ntdll() is api
        assert len(api.NtCreateFile.argtypes) == 11
        assert len(api.NtSetInformationFile.argtypes) == 5
    finally:
        handles.ntdll.cache_clear()
