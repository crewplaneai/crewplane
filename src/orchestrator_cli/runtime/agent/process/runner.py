from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from .diagnostics import PROCESS_EXIT_WARNING_MESSAGE
from .signals import reap_failed_process
from .streams import (
    close_log_handle,
    collect_process_output,
    write_stdin,
)

__all__ = [
    "PROCESS_EXIT_WARNING_MESSAGE",
    "build_log_header",
    "build_retry_log_header",
    "close_log_handle",
    "collect_process_output",
    "reap_failed_process",
    "write_stdin",
]


def build_log_header(
    cli_executable: str,
    model: str | None,
    output_file: Path,
) -> bytes:
    started_at = datetime.now(UTC).isoformat()
    model_label = model if model is not None else "provider default"
    header = (
        f"started_at: {started_at}\n"
        f"cli_executable: {cli_executable}\n"
        f"model: {model_label}\n"
        f"output_file: {output_file}\n"
        "---\n"
    )
    return header.encode("utf-8")


def build_retry_log_header(attempt_number: int) -> bytes:
    started_at = datetime.now(UTC).isoformat()
    header = f"\n---\nretry_attempt: {attempt_number}\nstarted_at: {started_at}\n---\n"
    return header.encode("utf-8")
