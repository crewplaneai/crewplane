import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.adapters.invokers.cli_invoker.capabilities import (
    build_cli_invocation_plan,
)
from crewplane.architecture.contracts import LogPresentationDescriptor
from crewplane.architecture.contracts.provider_log import (
    STDERR_PREFIX_BYTES,
    build_provider_log_header,
    encode_retry_log_header,
)
from crewplane.core.config import AgentConfig
from crewplane.observability.log_presentation import format_log_file
from crewplane.observability.log_presentation.limits import LogPresentationLimits
from crewplane.observability.log_presentation.tail import (
    find_initial_body_start,
    read_bounded_tail,
    read_latest_attempt_body,
)
from crewplane.observability.tmux.log_tail import read_log_tail
from crewplane.runtime.agent.process.stream_capture import CapturedStream
from crewplane.runtime.agent.process.streams import pipe_stream


@pytest.mark.parametrize("model", [None, "", "mødel"])
def test_adapter_header_is_consumed_by_both_log_readers(
    tmp_path: Path, model: str | None
) -> None:
    plan = build_cli_invocation_plan(
        AgentConfig(cli_cmd=[sys.executable], model_arg=None),
        model,
        "prompt",
        tmp_path / "résultat.md",
    )
    body = "visible café\n".encode()
    log_path = tmp_path / "provider.log"
    log_path.write_bytes(plan.log_header + body)

    result = read_bounded_tail(log_path, 0.0)

    assert result is not None
    assert result.body == body
    assert result.truncated is False
    assert result.started_mid_line is False
    assert read_log_tail(log_path, 10) == ["visible café"]


def test_log_readers_preserve_header_scan_limits(tmp_path: Path) -> None:
    header = build_provider_log_header("now", "cli", "m" * 4096, Path("out.md"))
    log_path = tmp_path / "provider.log"
    log_path.write_bytes(header + b"body\n")
    result = read_bounded_tail(log_path, 0.0)

    assert result is not None
    assert result.body == header + b"body\n"
    assert read_log_tail(log_path, 10)[0] == "started_at: now"
    assert find_initial_body_start(
        log_path,
        log_path.stat().st_size,
        LogPresentationLimits(header_scan_bytes=len(header)),
    ) == len(header)
    assert find_initial_body_start(log_path, 0) == 0


def test_log_readers_preserve_missing_file_and_open_failure_behavior(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "provider.log"
    assert read_bounded_tail(log_path, 0.0) is None
    assert read_log_tail(log_path, 10) == []
    log_path.write_bytes(b"body\n")

    with patch.object(Path, "open", side_effect=OSError("read failed")):
        assert read_bounded_tail(log_path, 0.0) is None
        assert find_initial_body_start(log_path, 5) == 0
        with pytest.raises(OSError, match="read failed"):
            read_log_tail(log_path, 10)


@pytest.mark.parametrize("retry_count", [0, 1, 2])
def test_produced_stderr_and_retry_frames_select_only_final_structured_attempt(
    tmp_path: Path, retry_count: int
) -> None:
    raw_stderr = "warning café\r\n".encode()

    async def capture_stderr() -> bytes:
        reader = asyncio.StreamReader()
        reader.feed_data(raw_stderr)
        reader.feed_eof()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        capture = CapturedStream()
        try:
            await pipe_stream(reader, queue, STDERR_PREFIX_BYTES, capture)
            capture.close()
            assert capture.path.read_bytes() == raw_stderr
            assert capture.tail_bytes == raw_stderr
            rendered = b""
            while not queue.empty():
                part = queue.get_nowait()
                assert part is not None
                rendered += part
            return rendered
        finally:
            capture.cleanup()

    rendered_stderr = asyncio.run(capture_stderr())
    assert rendered_stderr == "[stderr] warning café\r\n".encode()
    content = build_provider_log_header("now", "cli", None, Path("out.md"))
    for attempt in range(retry_count):
        content += b'{"result":"obsolete"}\n'
        content += encode_retry_log_header(attempt + 2, "2026-11-13T03:00:00+00:00")
    final_body = rendered_stderr + b'{"result":"final"}\n'
    content += final_body
    log_path = tmp_path / "provider.log"
    log_path.write_bytes(content)

    result = read_latest_attempt_body(log_path, 0.0)
    snapshot = format_log_file(
        log_path,
        LogPresentationDescriptor("json_object", "claude"),
        10,
        invocation_status="succeeded",
    )

    assert result is not None
    assert result.body == final_body
    assert result.truncated is False
    assert snapshot.lines == ("stderr: warning café", "result: final")
    assert snapshot.notices == ()
    assert log_path.read_bytes() == content
