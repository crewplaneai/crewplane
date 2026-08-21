from __future__ import annotations

import codecs

from ..retry_units import normalize_retry_wait_units_in_text


class IncrementalLogRenderer:
    def __init__(self, prefix: bytes) -> None:
        self._prefix = prefix
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._line_open = False
        self._previous_chunk_ended_with_carriage_return = False

    def render(self, chunk: bytes) -> bytes:
        text = self._decoder.decode(chunk, final=False)
        return self._render_text(text)

    def finish(self) -> bytes:
        text = self._decoder.decode(b"", final=True)
        return self._render_text(text)

    def _render_text(self, text: str) -> bytes:
        payloads: list[bytes] = []
        if self._previous_chunk_ended_with_carriage_return:
            if not text:
                return b""
            self._previous_chunk_ended_with_carriage_return = False
            if text.startswith("\n"):
                payloads.append(b"\n")
                text = text[1:]
        if not text:
            return b"".join(payloads)
        payload, self._line_open = render_log_text_segments(
            text,
            self._prefix,
            self._line_open,
        )
        self._previous_chunk_ended_with_carriage_return = text.endswith("\r")
        payloads.append(payload)
        return b"".join(payloads)


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
