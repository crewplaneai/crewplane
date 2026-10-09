import os
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from crewplane.architecture import safe_files_windows
from crewplane.observability.run_summary import event_log_io, event_log_windows


@pytest.mark.parametrize("failed", [False, True])
def test_windows_append_preserves_bytes_and_closes_stream_before_protection(
    tmp_path, monkeypatch, failed
):
    path = tmp_path / "events.ndjson"
    path.write_bytes(b"previous\n")
    descriptors = []
    failure = OSError("append failed")
    fdopen = os.fdopen

    @contextmanager
    def protected_writable_file(target, append=False):
        assert target == path and append
        with target.open("ab") as stream:
            descriptor = stream.fileno()
            descriptors.append(descriptor)
            yield descriptor
            os.fstat(descriptor)

    @contextmanager
    def writable_stream(descriptor, mode, closefd=True):
        assert descriptor == descriptors[0] and mode == "ab" and not closefd
        with fdopen(descriptor, mode, closefd=closefd) as stream:
            if failed:
                yield Mock(write=Mock(side_effect=failure))
            else:
                yield stream

    monkeypatch.setattr(
        event_log_io,
        "event_log_appender",
        lambda: event_log_windows.append_event_log_line,
    )
    monkeypatch.setattr(
        safe_files_windows, "open_writable_file", protected_writable_file
    )
    monkeypatch.setattr(event_log_windows.os, "fdopen", writable_stream)
    if failed:
        with pytest.raises(OSError) as caught:
            event_log_io.event_log_appender()(path, "café\r\n\x1a\n")
        assert caught.value is failure
    else:
        event_log_io.event_log_appender()(path, "café\r\n\x1a\n")
    assert path.read_bytes() == b"previous\n" + (
        b"" if failed else "café\r\n\x1a\n".encode()
    )
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
