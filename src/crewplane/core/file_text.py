from __future__ import annotations

import codecs
from pathlib import Path

STREAM_READ_BYTES = 65_536


def path_has_non_whitespace_text(path: Path) -> bool:
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    with path.open("rb") as handle:
        while chunk := handle.read(STREAM_READ_BYTES):
            if any(not char.isspace() for char in decoder.decode(chunk)):
                return True
    return any(not char.isspace() for char in decoder.decode(b"", final=True))


def path_decoded_character_count(path: Path) -> int:
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    char_count = 0
    with path.open("rb") as handle:
        while chunk := handle.read(STREAM_READ_BYTES):
            char_count += len(decoder.decode(chunk))
    return char_count + len(decoder.decode(b"", final=True))
