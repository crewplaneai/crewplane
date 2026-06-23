from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .limits import DEFAULT_LIMITS, LogPresentationLimits
from .models import LogReadResult

_INITIAL_HEADER_PREFIXES = (
    "started_at:",
    "cli_executable:",
    "model:",
    "output_file:",
)
_RETRY_MARKER = b"\n---\nretry_attempt:"


@dataclass(frozen=True)
class LatestAttemptBody:
    body: bytes
    truncated: bool


def read_bounded_tail(
    log_path: Path,
    wall_time_now: float,
    limits: LogPresentationLimits = DEFAULT_LIMITS,
    max_bytes: int | None = None,
) -> LogReadResult | None:
    try:
        stat_result = log_path.stat()
    except OSError:
        return None

    read_limit = max_bytes or limits.max_tail_bytes
    file_size = stat_result.st_size
    read_size = min(file_size, read_limit)
    offset = max(0, file_size - read_size)
    body_start = find_initial_body_start(log_path, file_size, limits)
    try:
        with log_path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(read_size)
            started_mid_line = _starts_mid_line(handle, offset, body_start, data)
    except OSError:
        return None

    if body_start and offset < body_start:
        data = data[body_start - offset :]
    return LogReadResult(
        size_bytes=file_size,
        updated_age_seconds=max(0.0, wall_time_now - stat_result.st_mtime),
        body=data,
        truncated=offset > body_start,
        started_mid_line=started_mid_line,
    )


def read_latest_attempt_body(
    log_path: Path,
    wall_time_now: float,
    limits: LogPresentationLimits = DEFAULT_LIMITS,
    max_bytes: int | None = None,
) -> LogReadResult | None:
    result = read_bounded_tail(
        log_path,
        wall_time_now,
        limits,
        max_bytes=max_bytes or limits.max_json_object_parse_bytes,
    )
    if result is None:
        return None
    latest_attempt = latest_attempt_bytes(result.body, result.truncated)
    return LogReadResult(
        size_bytes=result.size_bytes,
        updated_age_seconds=result.updated_age_seconds,
        body=latest_attempt.body,
        truncated=latest_attempt.truncated,
        started_mid_line=result.started_mid_line,
    )


def latest_attempt_bytes(body: bytes, truncated: bool = False) -> LatestAttemptBody:
    marker_index = body.rfind(_RETRY_MARKER)
    if marker_index == -1:
        return LatestAttemptBody(body=body, truncated=truncated)
    header_end = body.find(b"\n---\n", marker_index + len(_RETRY_MARKER))
    if header_end == -1:
        return LatestAttemptBody(body=body[marker_index:], truncated=truncated)
    return LatestAttemptBody(
        body=body[header_end + len(b"\n---\n") :],
        truncated=False,
    )


def _starts_mid_line(
    handle: BinaryIO,
    offset: int,
    body_start: int,
    data: bytes,
) -> bool:
    if offset <= body_start or not data or data.startswith((b"\n", b"\r")):
        return False
    try:
        handle.seek(offset - 1)
        previous = handle.read(1)
    except OSError:
        return True
    return previous not in {b"\n", b"\r"}


def find_initial_body_start(
    log_path: Path,
    file_size: int,
    limits: LogPresentationLimits = DEFAULT_LIMITS,
) -> int:
    read_size = min(limits.header_scan_bytes, file_size)
    if read_size <= 0:
        return 0
    try:
        with log_path.open("rb") as handle:
            head = handle.read(read_size)
    except OSError:
        return 0

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

    if len(header_lines) != len(_INITIAL_HEADER_PREFIXES):
        return 0
    for line, prefix in zip(header_lines, _INITIAL_HEADER_PREFIXES, strict=True):
        if not line.startswith(prefix):
            return 0
    return body_start
