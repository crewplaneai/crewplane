from __future__ import annotations

from datetime import UTC, datetime

from crewplane.architecture.contracts.provider_log import encode_retry_log_header

from .diagnostics import PROCESS_EXIT_WARNING_MESSAGE
from .signals import reap_failed_process
from .streams import (
    close_log_handle,
    collect_process_output,
    write_stdin,
    write_stdin_and_collect_output,
)

__all__ = [
    "PROCESS_EXIT_WARNING_MESSAGE",
    "build_retry_log_header",
    "close_log_handle",
    "collect_process_output",
    "reap_failed_process",
    "write_stdin",
    "write_stdin_and_collect_output",
]


def build_retry_log_header(attempt_number: int) -> bytes:
    started_at = datetime.now(UTC).isoformat()
    return encode_retry_log_header(attempt_number, started_at)
