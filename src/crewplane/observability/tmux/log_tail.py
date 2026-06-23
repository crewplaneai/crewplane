from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path

_LOG_HEADER_PREFIXES = (
    "started_at:",
    "cli_executable:",
    "model:",
    "output_file:",
)


@dataclass(frozen=True)
class LogSnapshot:
    size_bytes: int
    updated_age_seconds: float
    tail_lines: tuple[str, ...]


@dataclass(frozen=True)
class _TailChunkRead:
    buffer: str
    stop_reading: bool
    reached_header_boundary: bool = False


def read_log_snapshot(
    log_path: Path,
    line_count: int,
    wall_time_now: float,
) -> LogSnapshot | None:
    try:
        stat_result = log_path.stat()
    except OSError:
        return None

    return LogSnapshot(
        size_bytes=stat_result.st_size,
        updated_age_seconds=max(0.0, wall_time_now - stat_result.st_mtime),
        tail_lines=tuple(read_log_tail(log_path, line_count)),
    )


def read_log_tail(log_path: Path, line_count: int) -> list[str]:
    if line_count <= 0:
        return []

    try:
        file_size = log_path.stat().st_size
    except OSError:
        return []

    max_lines = max(1, line_count)
    body_start = _find_log_body_start(log_path, file_size)
    if file_size <= 64 * 1024:
        with log_path.open("rb") as handle:
            if body_start > 0:
                handle.seek(body_start)
            text = handle.read().decode("utf-8", errors="replace")
        return [line for line in text.splitlines() if line][-max_lines:]

    return _read_large_log_tail(log_path, file_size, body_start, max_lines)


def _read_large_log_tail(
    log_path: Path,
    file_size: int,
    body_start: int,
    max_lines: int,
) -> list[str]:
    use_header_delimiter = body_start > 0
    lines: deque[str] = deque()
    buffer = ""
    offset = file_size
    with log_path.open("rb") as handle:
        while offset > 0 and len(lines) < max_lines:
            read_size = min(8192, offset)
            offset -= read_size
            handle.seek(offset)
            chunk = handle.read(read_size)
            if not chunk:
                break
            buffer = chunk.decode("utf-8", errors="replace") + buffer
            read_result = _consume_tail_chunk(
                lines=lines,
                buffer=buffer,
                offset=offset,
                body_start=body_start,
                max_lines=max_lines,
                use_header_delimiter=use_header_delimiter,
            )
            buffer = read_result.buffer
            if read_result.reached_header_boundary:
                offset = 0
            if read_result.stop_reading:
                break

    if buffer and len(lines) < max_lines:
        line = buffer.rstrip("\r\n")
        if line and (not use_header_delimiter or offset >= body_start):
            lines.appendleft(line)

    return list(lines)


def _consume_tail_chunk(
    lines: deque[str],
    buffer: str,
    offset: int,
    body_start: int,
    max_lines: int,
    use_header_delimiter: bool,
) -> _TailChunkRead:
    while True:
        newline_index = buffer.rfind("\n")
        if newline_index == -1:
            return _TailChunkRead(buffer=buffer, stop_reading=False)
        line = buffer[newline_index + 1 :].rstrip("\r")
        buffer = buffer[:newline_index]
        if not line:
            continue
        line_start_offset = offset + newline_index + 1
        if use_header_delimiter and line_start_offset < body_start:
            if line.strip() == "---":
                return _TailChunkRead(
                    buffer="",
                    stop_reading=True,
                    reached_header_boundary=True,
                )
            if line.strip():
                lines.appendleft(line)
            continue
        lines.appendleft(line)
        if len(lines) >= max_lines:
            return _TailChunkRead(buffer=buffer, stop_reading=True)


def _find_log_body_start(log_path: Path, file_size: int) -> int:
    read_size = min(4096, file_size)
    if read_size <= 0:
        return 0
    with log_path.open("rb") as handle:
        head = handle.read(read_size)
    header_lines: list[str] = []
    body_start = 0
    for raw_line in head.splitlines(keepends=True):
        stripped = raw_line.rstrip(b"\r\n")
        body_start += len(raw_line)
        if stripped == b"---":
            break
        if stripped:
            header_lines.append(stripped.decode("utf-8", errors="replace").strip())
    else:
        return 0

    if len(header_lines) != len(_LOG_HEADER_PREFIXES):
        return 0
    if any(
        not line.startswith(prefix)
        for line, prefix in zip(header_lines, _LOG_HEADER_PREFIXES, strict=True)
    ):
        return 0
    return body_start
