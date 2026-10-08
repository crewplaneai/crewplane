"""Native NTFS junction mutation without replacing the directory entry."""

import ctypes
import struct
from ctypes import wintypes

from crewplane.architecture.windows_file_handles import (
    kernel32,
    open_handle,
    path_error,
)


def set_junction(directory, target):
    substitute = ("\\??\\" + str(target.resolve())).encode("utf-16-le")
    display = str(target.resolve()).encode("utf-16-le")
    names = substitute + b"\0\0" + display + b"\0\0"
    data = (
        struct.pack(
            "<IHHHHHH",
            0xA0000003,
            8 + len(names),
            0,
            0,
            len(substitute),
            len(substitute) + 2,
            len(display),
        )
        + names
    )
    buffer = ctypes.create_string_buffer(data)
    api = kernel32()
    api.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    api.DeviceIoControl.restype = wintypes.BOOL
    handle = open_handle(directory, 0x100, 7)  # FILE_WRITE_ATTRIBUTES alone.
    try:
        if not api.DeviceIoControl(
            handle.value,
            0x900A4,
            buffer,
            len(data),
            None,
            0,
            ctypes.byref(wintypes.DWORD()),
            None,
        ):
            raise path_error(directory)
    finally:
        handle.close()
