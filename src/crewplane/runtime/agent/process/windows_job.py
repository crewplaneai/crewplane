"""Private, non-inheritable Job Object ownership for provider descendants."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from functools import cache
from typing import cast


class BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64),
        ("job_time", ctypes.c_int64),
        ("flags", wintypes.DWORD),
        ("minimum_working_set", ctypes.c_size_t),
        ("maximum_working_set", ctypes.c_size_t),
        ("active_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD),
    ]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", BasicLimits),
        ("io_counters", ctypes.c_uint64 * 6),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


class Accounting(ctypes.Structure):
    _fields_ = [
        ("user_time", ctypes.c_int64),
        ("kernel_time", ctypes.c_int64),
        ("period_user_time", ctypes.c_int64),
        ("period_kernel_time", ctypes.c_int64),
        ("page_faults", wintypes.DWORD),
        ("total", wintypes.DWORD),
        ("active", wintypes.DWORD),
        ("terminated", wintypes.DWORD),
    ]


@cache
def kernel32() -> ctypes.CDLL:
    if os.name != "nt":
        raise RuntimeError("Job Objects require native Windows.")
    api = cast(ctypes.CDLL, vars(ctypes)["WinDLL"]("kernel32", use_last_error=True))
    api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    api.CreateJobObjectW.restype = wintypes.HANDLE
    api.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    api.SetInformationJobObject.restype = wintypes.BOOL
    api.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    api.QueryInformationJobObject.restype = wintypes.BOOL
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    api.AssignProcessToJobObject.restype = wintypes.BOOL
    api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    api.TerminateJobObject.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    return api


class WindowsJob:
    def __init__(self) -> None:
        api = kernel32()
        handle = api.CreateJobObjectW(None, None)
        if not handle:
            raise cast(
                OSError, vars(ctypes)["WinError"](int(vars(ctypes)["get_last_error"]()))
            )
        self._handle = int(handle)
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not api.SetInformationJobObject(
            self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            error = cast(
                OSError, vars(ctypes)["WinError"](int(vars(ctypes)["get_last_error"]()))
            )
            self.close()
            raise error

    def assign(self, pid: int) -> None:
        api = kernel32()
        process = api.OpenProcess(0x0100 | 0x0001, False, pid)
        if not process:
            raise cast(
                OSError, vars(ctypes)["WinError"](int(vars(ctypes)["get_last_error"]()))
            )
        try:
            if not api.AssignProcessToJobObject(self._handle, process):
                error = cast(
                    OSError,
                    vars(ctypes)["WinError"](int(vars(ctypes)["get_last_error"]())),
                )
                error.add_note(
                    "Provider launch was withheld. Check incompatible enclosing Job Object restrictions."
                )
                raise error
        finally:
            if not api.CloseHandle(process):
                raise cast(
                    OSError,
                    vars(ctypes)["WinError"](int(vars(ctypes)["get_last_error"]())),
                )

    def active_process_count(self) -> int:
        info = Accounting()
        if not kernel32().QueryInformationJobObject(
            self._handle, 1, ctypes.byref(info), ctypes.sizeof(info), None
        ):
            raise cast(
                OSError, vars(ctypes)["WinError"](int(vars(ctypes)["get_last_error"]()))
            )
        return int(info.active)

    def terminate(self) -> None:
        if not kernel32().TerminateJobObject(self._handle, 1):
            raise cast(
                OSError, vars(ctypes)["WinError"](int(vars(ctypes)["get_last_error"]()))
            )

    def close(self) -> None:
        if self._handle:
            handle, self._handle = self._handle, 0
            if not kernel32().CloseHandle(handle):
                raise cast(
                    OSError,
                    vars(ctypes)["WinError"](int(vars(ctypes)["get_last_error"]())),
                )
