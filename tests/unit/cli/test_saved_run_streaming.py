from __future__ import annotations

import io
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from crewplane.cli.runs import streaming
from tests.helpers.platforms import symlink_or_skip


def make_log(tmp_path: Path, content: bytes) -> Path:
    path = tmp_path / "logs" / "provider.log"
    path.parent.mkdir()
    path.write_bytes(content)
    return path


@pytest.mark.parametrize(
    "payload", [b"[literal]\r\n\nend\r\n", "hello café 世界\n".encode()]
)
def test_stream_preserves_raw_text(tmp_path: Path, payload: bytes) -> None:
    path = make_log(tmp_path, payload)
    output = io.StringIO(newline="")
    streaming.stream_saved_log(tmp_path, path, output=output)
    assert output.getvalue().encode() == payload


def test_follow_emits_appends_once_and_waits_at_eof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_log(tmp_path, b"initial\n")
    output = io.StringIO(newline="")
    appends = iter([b"next\r\n", b"", b"last\n"])
    polls: list[str] = []

    def poll(seconds: float) -> None:
        assert seconds > 0
        polls.append(output.getvalue())
        try:
            chunk = next(appends)
        except StopIteration:
            raise KeyboardInterrupt from None
        with path.open("ab") as target:
            target.write(chunk)

    monkeypatch.setattr(streaming.time, "sleep", poll)
    streaming.stream_saved_log(tmp_path, path, True, output)
    assert output.getvalue() == "initial\nnext\r\nlast\n"
    assert polls == [
        "initial\n",
        "initial\nnext\r\n",
        "initial\nnext\r\n",
        "initial\nnext\r\nlast\n",
    ]


def test_utf8_decoder_retains_split_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_log(tmp_path, b"a\xe2")
    output = io.StringIO()
    appends = iter([b"\x82", b"\xac!\n"])

    def poll(seconds: float) -> None:
        assert seconds > 0
        try:
            chunk = next(appends)
        except StopIteration:
            raise KeyboardInterrupt from None
        with path.open("ab") as target:
            target.write(chunk)

    monkeypatch.setattr(streaming.time, "sleep", poll)
    streaming.stream_saved_log(tmp_path, path, True, output)
    assert output.getvalue() == "a€!\n"


def test_utf8_decoder_retains_split_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = "€世界\n".encode()
    path = make_log(tmp_path, payload)
    output = io.StringIO()
    original = os.read

    def small_read(descriptor: int, size: int) -> bytes:
        return original(descriptor, min(size, 1))

    monkeypatch.setattr(streaming.os, "read", small_read)
    streaming.stream_saved_log(tmp_path, path, output=output)
    assert output.getvalue().encode() == payload


@pytest.mark.parametrize(
    "change", ["delete", "replace", "truncate", "hardlink", "parent-link"]
)
def test_follow_rejects_changed_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    path = make_log(tmp_path, b"original\n")
    output = io.StringIO()

    def poll(seconds: float) -> None:
        assert seconds > 0
        if change == "delete":
            path.unlink()
        elif change == "replace":
            replacement = tmp_path / "replacement"
            replacement.write_bytes(b"replacement\n")
            replacement.replace(path)
        elif change == "truncate":
            path.write_bytes(b"")
        elif change == "hardlink":
            os.link(path, tmp_path / "alias")
        else:
            path.parent.rename(tmp_path / "moved")
            symlink_or_skip(path.parent, tmp_path / "moved", target_is_directory=True)

    monkeypatch.setattr(streaming.time, "sleep", poll)
    expected = {
        "delete": "deleted",
        "replace": "replaced",
        "truncate": "truncated",
    }.get(change, "unsafe")
    with pytest.raises(streaming.SavedLogStreamError, match=expected):
        streaming.stream_saved_log(tmp_path, path, True, output)
    assert output.getvalue() == "original\n"


@pytest.mark.parametrize("content", [b"\xff", b"partial\xe2"])
def test_invalid_or_incomplete_utf8_is_an_error(tmp_path: Path, content: bytes) -> None:
    path = make_log(tmp_path, content)
    with pytest.raises(streaming.SavedLogStreamError, match="Cannot safely read"):
        streaming.stream_saved_log(tmp_path, path, output=io.StringIO())


def test_missing_log_fails_without_waiting(tmp_path: Path) -> None:
    with pytest.raises(streaming.SavedLogStreamError, match="missing"):
        streaming.stream_saved_log(tmp_path, tmp_path / "missing", True, io.StringIO())
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "failure", ["interrupt", "broken-pipe-write", "broken-pipe-flush", "read", "write"]
)
def test_closes_descriptor_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    path = make_log(tmp_path, b"content")
    original = streaming.open_regular_file
    descriptors: list[int] = []

    @contextmanager
    def track_open(selected: Path) -> Iterator[int]:
        with original(selected) as descriptor:
            descriptors.append(descriptor)
            yield descriptor

    class FailingOutput(io.StringIO):
        def write(self, text: str) -> int:
            if failure == "broken-pipe-write":
                raise BrokenPipeError
            if failure == "write":
                raise OSError("output failure")
            return super().write(text)

        def flush(self) -> None:
            if failure == "broken-pipe-flush":
                raise BrokenPipeError
            super().flush()

    def stop(seconds: float) -> None:
        assert seconds > 0
        raise KeyboardInterrupt

    def fail_read(descriptor: int, size: int) -> bytes:
        assert descriptor >= 0 and size > 0
        raise OSError("read failure")

    monkeypatch.setattr(streaming, "open_regular_file", track_open)
    monkeypatch.setattr(streaming.time, "sleep", stop)
    if failure == "read":
        monkeypatch.setattr(streaming.os, "read", fail_read)
    output = FailingOutput()
    if failure in {"read", "write"}:
        with pytest.raises(streaming.SavedLogStreamError):
            streaming.stream_saved_log(tmp_path, path, True, output)
    else:
        streaming.stream_saved_log(tmp_path, path, True, output)
    assert not output.closed
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


@pytest.mark.parametrize(
    "kind", ["symlink", "hardlink", "directory", "outside", "traversal"]
)
def test_rejects_unsafe_initial_paths(tmp_path: Path, kind: str) -> None:
    path = make_log(tmp_path, b"data")
    if kind == "symlink":
        link = tmp_path / "link"
        symlink_or_skip(link, path)
        path = link
    elif kind == "hardlink":
        os.link(path, tmp_path / "alias")
    elif kind == "directory":
        path = path.parent
    elif kind == "outside":
        path = tmp_path.parent / "outside"
    else:
        path = tmp_path / "logs" / ".." / "logs" / "provider.log"
    with pytest.raises(streaming.SavedLogStreamError):
        streaming.stream_saved_log(tmp_path, path, output=io.StringIO())


def test_follow_stays_on_selected_file_when_other_logs_appear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make_log(tmp_path, b"selected\n")
    output = io.StringIO()
    polls = 0

    def poll(seconds: float) -> None:
        nonlocal polls
        assert seconds > 0
        polls += 1
        if polls > 1:
            raise KeyboardInterrupt
        (path.parent / "new-round.log").write_bytes(b"not selected\n")
        with path.open("ab") as target:
            target.write(b"still selected\n")

    monkeypatch.setattr(streaming.time, "sleep", poll)
    streaming.stream_saved_log(tmp_path, path, True, output)
    assert output.getvalue() == "selected\nstill selected\n"
