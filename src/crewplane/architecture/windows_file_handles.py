"""Private Win32 handle ownership for local filesystem operations."""

from __future__ import annotations

import ctypes
import ntpath
import os
from collections.abc import Iterator
from ctypes import wintypes
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import cast

FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_READ_ATTRIBUTES = 0x80
FILE_TRAVERSE = 0x20
FILE_LIST_DIRECTORY = 1
DELETE = 0x10000
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 1
FILE_SHARE_WRITE = 2
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_OPEN = 1
FILE_CREATE = 2
FILE_OPEN_IF = 3


class UnicodeString(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint16),
        ("maximum_length", ctypes.c_uint16),
        ("buffer", ctypes.c_void_p),
    ]


class ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint32),
        ("root", wintypes.HANDLE),
        ("name", ctypes.POINTER(UnicodeString)),
        ("attributes", ctypes.c_uint32),
        ("security", ctypes.c_void_p),
        ("quality", ctypes.c_void_p),
    ]


class IoStatusBlock(ctypes.Structure):
    _fields_ = [("status", ctypes.c_void_p), ("information", ctypes.c_size_t)]


class EntryInformation(ctypes.Structure):
    _fields_ = [
        ("replace", ctypes.c_ubyte),
        ("root", wintypes.HANDLE),
        ("length", ctypes.c_uint32),
        ("name", ctypes.c_uint16 * 1),
    ]


class DirectoryInformation(ctypes.Structure):
    _fields_ = [
        ("next_offset", ctypes.c_uint32),
        ("index", ctypes.c_uint32),
        ("times_and_sizes", ctypes.c_int64 * 6),
        ("attributes", ctypes.c_uint32),
        ("name_length", ctypes.c_uint32),
        ("extended_attributes", ctypes.c_uint32),
        ("short_length", ctypes.c_byte),
        ("short_name", ctypes.c_uint16 * 12),
        ("identity", ctypes.c_int64),
        ("name", ctypes.c_uint16 * 1),
    ]


class FileInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("created", wintypes.FILETIME),
        ("accessed", wintypes.FILETIME),
        ("written", wintypes.FILETIME),
        ("volume", wintypes.DWORD),
        ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD),
        ("links", wintypes.DWORD),
        ("index_high", wintypes.DWORD),
        ("index_low", wintypes.DWORD),
    ]

    @property
    def identity(self) -> tuple[int, int]:
        return self.volume, self.index_high << 32 | self.index_low


@cache
def kernel32() -> ctypes.CDLL:
    if os.name != "nt":
        raise RuntimeError("Windows file handles require native Windows.")
    api = cast(ctypes.CDLL, vars(ctypes)["WinDLL"]("kernel32", use_last_error=True))
    api.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    api.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(FileInformation),
    ]
    api.GetFileInformationByHandle.restype = wintypes.BOOL
    api.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    api.GetFileInformationByHandleEx.restype = wintypes.BOOL
    api.GetFileType.argtypes = [wintypes.HANDLE]
    api.GetFileType.restype = wintypes.DWORD
    api.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    api.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    api.GetLongPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    api.GetLongPathNameW.restype = wintypes.DWORD
    api.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    api.SetFileInformationByHandle.restype = wintypes.BOOL
    return api


@cache
def ntdll() -> ctypes.CDLL:
    if os.name != "nt":
        raise RuntimeError("Windows file handles require native Windows.")
    api = cast(ctypes.CDLL, vars(ctypes)["WinDLL"]("ntdll", use_last_error=True))
    api.NtCreateFile.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.c_uint32,
        ctypes.POINTER(ObjectAttributes),
        ctypes.POINTER(IoStatusBlock),
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    api.NtCreateFile.restype = ctypes.c_int32
    api.NtSetInformationFile.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(IoStatusBlock),
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    api.NtSetInformationFile.restype = ctypes.c_int32
    api.RtlNtStatusToDosError.argtypes = [ctypes.c_int32]
    api.RtlNtStatusToDosError.restype = ctypes.c_uint32
    return api


def _status_error(status: int, path: Path) -> OSError:
    code = int(ntdll().RtlNtStatusToDosError(status))
    error = cast(OSError, vars(ctypes)["WinError"](code))
    error.filename = str(path)
    return error


def _entry_name(name: str) -> bytes:
    if name in {"", ".", ".."} or any(char in name for char in "\\/:\0"):
        raise ValueError(f"Protected operations require a single entry name: {name!r}")
    encoded = name.encode("utf-16-le")
    if len(encoded) > 65532:
        raise ValueError("Protected entry name is too long.")
    return encoded


def path_error(path: Path) -> OSError:
    code = int(vars(ctypes)["get_last_error"]())
    error = cast(OSError, vars(ctypes)["WinError"](code))
    error.filename = str(path)
    if code == 206:
        error.add_note("Enable Windows long-path support or shorten the project path.")
    return error


def descriptor_identity(descriptor: int, path: Path) -> tuple[int, int]:
    """Inspect a CRT-owned handle without transferring or closing ownership."""
    import msvcrt

    value = int(vars(msvcrt)["get_osfhandle"](descriptor))
    info = FileInformation()
    if not kernel32().GetFileInformationByHandle(value, ctypes.byref(info)):
        raise path_error(path)
    return info.identity


@dataclass
class FileHandle:
    value: int
    path: Path

    def open_child(
        self,
        name: str,
        access: int = FILE_READ_ATTRIBUTES,
        share: int = FILE_SHARE_READ,
        disposition: int = FILE_OPEN,
        directory: bool | None = None,
    ) -> FileHandle:
        encoded = _entry_name(name)
        buffer = ctypes.create_string_buffer(encoded + b"\0\0")
        text = UnicodeString(len(encoded), len(encoded) + 2, ctypes.addressof(buffer))
        attributes = ObjectAttributes(
            ctypes.sizeof(ObjectAttributes),
            self.value,
            ctypes.pointer(text),
            0x40,
            None,
            None,
        )
        value, io = wintypes.HANDLE(), IoStatusBlock()
        # Relative single-component lookup never reparses the validated parent.
        options = 0x20 | 0x4000 | 0x200000
        if directory is not None:
            options |= 1 if directory else 0x40
        status = ntdll().NtCreateFile(
            ctypes.byref(value),
            access | 0x100000,
            ctypes.byref(attributes),
            ctypes.byref(io),
            None,
            0,
            share,
            disposition,
            options,
            None,
            0,
        )
        if status < 0:
            raise _status_error(status, self.path / name)
        return FileHandle(cast(int, value.value), self.path / name)

    def entry_names(self) -> Iterator[str]:
        buffer = ctypes.create_string_buffer(65536)
        information_class = 11  # FileIdBothDirectoryRestartInfo, then continuation.
        while kernel32().GetFileInformationByHandleEx(
            self.value, information_class, buffer, len(buffer)
        ):
            information_class = 10
            offset = 0
            while True:
                info = DirectoryInformation.from_buffer(buffer, offset)
                start = offset + DirectoryInformation.name.offset
                name = buffer.raw[start : start + info.name_length].decode("utf-16-le")
                if name not in {".", ".."}:
                    yield name
                if not info.next_offset:
                    break
                offset += info.next_offset
        if int(vars(ctypes)["get_last_error"]()) != 18:  # ERROR_NO_MORE_FILES.
            raise path_error(self.path)

    def publish_entry(
        self, parent: FileHandle, name: str, rename: bool = False
    ) -> None:
        encoded = _entry_name(name)
        size = max(
            ctypes.sizeof(EntryInformation), EntryInformation.name.offset + len(encoded)
        )
        buffer = ctypes.create_string_buffer(size)
        info = EntryInformation.from_buffer(buffer)
        info.replace = rename
        info.root = parent.value
        info.length = len(encoded)
        ctypes.memmove(
            ctypes.addressof(buffer) + EntryInformation.name.offset,
            encoded,
            len(encoded),
        )
        status = ntdll().NtSetInformationFile(
            self.value,
            ctypes.byref(IoStatusBlock()),
            buffer,
            len(buffer),
            10 if rename else 11,
        )
        if status < 0:
            raise _status_error(status, parent.path / name)

    def close(self) -> None:
        if self.value:
            value, self.value = self.value, 0
            if not kernel32().CloseHandle(value):
                raise path_error(self.path)

    def information(self) -> FileInformation:
        info = FileInformation()
        if not kernel32().GetFileInformationByHandle(self.value, ctypes.byref(info)):
            raise path_error(self.path)
        return info

    def validate(self, directory: bool, links: int = 1) -> FileInformation:
        info = self.information()
        if info.attributes & FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(
                f"Reparse points are forbidden in protected paths: {self.path}"
            )
        if bool(info.attributes & FILE_ATTRIBUTE_DIRECTORY) != directory:
            raise ValueError(f"Unexpected file type in protected path: {self.path}")
        if not directory and (
            kernel32().GetFileType(self.value) != 1 or info.links != links
        ):
            raise ValueError(
                f"Protected source must be a regular file with {links} link(s): {self.path}"
            )
        expected = _unextended_path(os.path.abspath(self.path))
        resolved = ntpath.normcase(self.final_path())
        if resolved != ntpath.normcase(expected) and resolved != ntpath.normcase(
            _long_path(self.path)
        ):
            raise ValueError(
                f"Protected handle resolves outside its expected path: {self.path}"
            )
        return info

    def final_path(self) -> str:
        api = kernel32()
        needed = api.GetFinalPathNameByHandleW(self.value, None, 0, 0)
        if not needed:
            raise path_error(self.path)
        buffer = ctypes.create_unicode_buffer(needed + 1)
        length = api.GetFinalPathNameByHandleW(self.value, buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise path_error(self.path)
        return _unextended_path(buffer.value)

    def into_descriptor(self, flags: int = os.O_RDONLY) -> int:
        import msvcrt

        descriptor = int(
            vars(msvcrt)["open_osfhandle"](self.value, flags | vars(os)["O_BINARY"])
        )
        self.value = 0
        return descriptor

    def delete(self) -> None:
        # FileDispositionInfo removes the opened entry, never a competing pathname.
        disposition = wintypes.BOOL(True)
        if not kernel32().SetFileInformationByHandle(
            self.value, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)
        ):
            raise path_error(self.path)


def open_handle(
    path: Path, access: int = FILE_READ_ATTRIBUTES, share: int = FILE_SHARE_READ
) -> FileHandle:
    value = kernel32().CreateFileW(
        str(path),
        access,
        share,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if value == ctypes.c_void_p(-1).value:
        raise path_error(path)
    return FileHandle(int(value), path)


def _unextended_path(value: str) -> str:
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    return value.removeprefix("\\\\?\\")


def _long_path(path: Path) -> str:
    value = _unextended_path(os.path.abspath(path))
    extended = (
        "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
    )
    api = kernel32()
    needed = api.GetLongPathNameW(extended, None, 0)
    if not needed:
        raise path_error(path)
    buffer = ctypes.create_unicode_buffer(needed)
    length = api.GetLongPathNameW(extended, buffer, len(buffer))
    if not length or length >= len(buffer):
        raise path_error(path)
    return _unextended_path(buffer.value)
