import ctypes
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from crewplane.runtime.agent.process import windows_job


@pytest.fixture
def api(monkeypatch):
    native = Mock()
    native.CreateJobObjectW.return_value = 101
    native.OpenProcess.return_value = 202
    for operation in (
        "SetInformationJobObject",
        "QueryInformationJobObject",
        "AssignProcessToJobObject",
        "TerminateJobObject",
        "CloseHandle",
    ):
        getattr(native, operation).return_value = True
    monkeypatch.setattr(windows_job, "kernel32", Mock(return_value=native))
    monkeypatch.setattr(
        ctypes, "WinError", lambda code: OSError(code, "native failure"), raising=False
    )
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    return native


def test_job_is_noninheritable_kill_on_close_and_closes_process_handle(api):
    observed_flags = []

    def configure(handle, info_class, pointer, size):
        assert (
            handle == 101
            and info_class == 9
            and size == ctypes.sizeof(windows_job.ExtendedLimits)
        )
        observed_flags.append(
            ctypes.cast(
                pointer, ctypes.POINTER(windows_job.ExtendedLimits)
            ).contents.basic.flags
        )
        return True

    def query(handle, info_class, pointer, size, returned):
        assert handle == 101 and info_class == 1 and size and returned is None
        ctypes.cast(pointer, ctypes.POINTER(windows_job.Accounting)).contents.active = 3
        return True

    api.SetInformationJobObject.side_effect = configure
    api.QueryInformationJobObject.side_effect = query
    job = windows_job.WindowsJob()
    api.CreateJobObjectW.assert_called_once_with(None, None)
    assert observed_flags == [0x2000]
    job.assign(999)
    api.OpenProcess.assert_called_once_with(0x101, False, 999)
    api.AssignProcessToJobObject.assert_called_once_with(101, 202)
    assert api.CloseHandle.call_args.args == (202,)
    assert job.active_process_count() == 3
    job.terminate()
    api.TerminateJobObject.assert_called_once_with(101, 1)
    job.close()
    job.close()
    assert api.CloseHandle.call_count == 2


@pytest.mark.parametrize(
    "operation",
    [
        "CreateJobObjectW",
        "SetInformationJobObject",
        "OpenProcess",
        "AssignProcessToJobObject",
        "QueryInformationJobObject",
        "TerminateJobObject",
        "CloseHandle",
    ],
)
def test_native_ownership_failures_are_explicit(api, operation):
    if operation in {"CreateJobObjectW", "SetInformationJobObject"}:
        getattr(api, operation).return_value = 0
        with pytest.raises(OSError):
            windows_job.WindowsJob()
        if operation == "SetInformationJobObject":
            api.CloseHandle.assert_called_once_with(101)
        return
    job = windows_job.WindowsJob()
    getattr(api, operation).return_value = 0
    with pytest.raises(OSError):
        if operation in {"OpenProcess", "AssignProcessToJobObject"}:
            job.assign(999)
        elif operation == "QueryInformationJobObject":
            job.active_process_count()
        elif operation == "TerminateJobObject":
            job.terminate()
        else:
            job.close()
    if operation == "AssignProcessToJobObject":
        api.CloseHandle.assert_called_once_with(202)
    api.CloseHandle.return_value = True
    job.close()


def test_binding_configuration_is_lazy_and_platform_guarded(monkeypatch):
    windows_job.kernel32.cache_clear()
    monkeypatch.setattr(windows_job, "os", SimpleNamespace(name="posix"))
    with pytest.raises(RuntimeError, match="native Windows"):
        windows_job.kernel32()
    native = Mock()
    factory = Mock(return_value=native)
    monkeypatch.setattr(windows_job, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(ctypes, "WinDLL", factory, raising=False)
    try:
        assert windows_job.kernel32() is native
        assert native.CreateJobObjectW.restype is ctypes.wintypes.HANDLE
        assert len(native.AssignProcessToJobObject.argtypes) == 2
    finally:
        windows_job.kernel32.cache_clear()
