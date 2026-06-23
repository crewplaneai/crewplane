from __future__ import annotations

from ..retry_units import normalize_retry_wait_units_in_text


def render_log_text_segments(
    text: str,
    prefix: bytes,
    line_open: bool,
) -> tuple[bytes, bool]:
    payloads: list[bytes] = []
    current_line_open = line_open
    for segment in text.splitlines(keepends=True):
        if prefix and not current_line_open:
            payloads.append(prefix)
        payloads.append(normalize_retry_wait_units_in_text(segment).encode("utf-8"))
        current_line_open = not segment.endswith(("\n", "\r"))
    return b"".join(payloads), current_line_open
