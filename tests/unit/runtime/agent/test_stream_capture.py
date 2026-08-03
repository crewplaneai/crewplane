from pathlib import Path

import pytest

from crewplane.runtime.agent.process.stream_capture import (
    CapturedStream,
    ProcessOutputCapture,
    ProcessStreamCapture,
)


def test_process_stream_capture_reads_persisted_lines_with_replacement(
    tmp_path: Path,
) -> None:
    path = tmp_path / "stream.bin"
    path.write_bytes(b"first\ninvalid: \xff\n")
    capture = ProcessStreamCapture(path=path, tail_bytes=b"ignored")

    assert list(capture.iter_lines()) == ["first", "invalid: �"]


def test_process_stream_capture_uses_tail_when_file_is_missing(tmp_path: Path) -> None:
    capture = ProcessStreamCapture(
        path=tmp_path / "missing",
        tail_bytes=b"first\nsecond\n",
    )

    assert list(capture.iter_lines()) == ["first", "second"]
    assert (
        list(
            ProcessStreamCapture(path=tmp_path / "missing", tail_bytes=b"").iter_lines()
        )
        == []
    )


def test_process_stream_cleanup_removes_file_and_suppresses_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "stream.bin"
    path.write_bytes(b"data")
    ProcessStreamCapture(path=path, tail_bytes=b"").cleanup()
    assert not path.exists()

    def fail_unlink(self: Path, missing_ok: bool = False) -> None:
        del self, missing_ok
        raise OSError("blocked")

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    ProcessStreamCapture(path=path, tail_bytes=b"").cleanup()


def test_captured_stream_rejects_negative_memory_limit() -> None:
    with pytest.raises(ValueError, match="must be nonnegative"):
        CapturedStream(max_memory_bytes=-1)


def test_captured_stream_discards_whole_and_partial_overflow_chunks() -> None:
    capture = CapturedStream(max_memory_bytes=5)
    try:
        capture.write(b"ab")
        capture.write(b"cd")
        capture.write(b"ef")

        assert capture.tail_bytes == b"bcdef"
        capture.write(b"g")
        assert capture.tail_bytes == b"cdefg"
        assert capture.path.exists()
        capture.close()
        capture.close()
    finally:
        capture.cleanup()


def test_process_output_capture_exposes_tails_iteration_and_cleanup(
    tmp_path: Path,
) -> None:
    stdout_path = tmp_path / "stdout"
    stderr_path = tmp_path / "stderr"
    stdout_path.write_bytes(b"out")
    stderr_path.write_bytes(b"err")
    capture = ProcessOutputCapture(
        stdout=ProcessStreamCapture(stdout_path, b"out-tail"),
        stderr=ProcessStreamCapture(stderr_path, b"err-tail"),
    )

    assert tuple(capture) == (b"out-tail", b"err-tail")
    assert capture.stdout_tail == b"out-tail"
    assert capture.stderr_tail == b"err-tail"
    capture.cleanup()
    assert not stdout_path.exists()
    assert not stderr_path.exists()
