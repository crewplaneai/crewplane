"""Initial provider-log encoding and byte-offset recognition."""

from __future__ import annotations

from pathlib import Path

_STARTED_AT_PREFIX = "started_at:"
_CLI_EXECUTABLE_PREFIX = "cli_executable:"
_MODEL_PREFIX = "model:"
_OUTPUT_FILE_PREFIX = "output_file:"
_PROVIDER_LOG_HEADER_PREFIXES = (
    _STARTED_AT_PREFIX,
    _CLI_EXECUTABLE_PREFIX,
    _MODEL_PREFIX,
    _OUTPUT_FILE_PREFIX,
)
_OPTIONAL_REASONING_PREFIX = "requested_reasoning:"


def build_provider_log_header(
    started_at: str,
    cli_executable: str,
    model: str | None,
    output_file: Path,
    requested_reasoning: str | None = None,
) -> bytes:
    model_label = model if model is not None else "provider default"
    reasoning_line = (
        f"{_OPTIONAL_REASONING_PREFIX} {requested_reasoning}\n"
        if requested_reasoning is not None
        else ""
    )
    header = (
        f"{_STARTED_AT_PREFIX} {started_at}\n"
        f"{_CLI_EXECUTABLE_PREFIX} {cli_executable}\n"
        f"{_MODEL_PREFIX} {model_label}\n"
        f"{reasoning_line}"
        f"{_OUTPUT_FILE_PREFIX} {output_file}\n"
        "---\n"
    )
    return header.encode("utf-8")


def provider_log_body_start(head: bytes) -> int:
    """Return the byte offset after a valid provider-log header, or zero."""

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

    if len(header_lines) == len(_PROVIDER_LOG_HEADER_PREFIXES) + 1 and header_lines[
        3
    ].startswith(_OPTIONAL_REASONING_PREFIX):
        header_lines.pop(3)
    if len(header_lines) != len(_PROVIDER_LOG_HEADER_PREFIXES):
        return 0
    if any(
        not line.startswith(prefix)
        for line, prefix in zip(
            header_lines,
            _PROVIDER_LOG_HEADER_PREFIXES,
            strict=True,
        )
    ):
        return 0
    return body_start
