from __future__ import annotations

_PROVIDER_LOG_HEADER_PREFIXES = (
    "started_at:",
    "cli_executable:",
    "model:",
    "output_file:",
)
_OPTIONAL_REASONING_PREFIX = "requested_reasoning:"


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
