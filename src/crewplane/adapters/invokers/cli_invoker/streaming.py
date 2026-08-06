from __future__ import annotations

import codecs
import json
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from tempfile import NamedTemporaryFile

from crewplane.architecture.contracts import CommandResult

# A 64K read size amortizes filesystem calls while bounding each read. Parsing
# and incremental decoding must remain correct across arbitrary boundaries.
STREAM_READ_BYTES = 64 * 1024


def load_stdout_json(
    result: CommandResult,
) -> tuple[Mapping[str, object] | None, str | None]:
    source = stdout_source(result)
    if source is None:
        return None, None
    try:
        payload = json.loads("".join(source))
    except json.JSONDecodeError as exc:
        return None, f"Malformed machine-readable output: {exc.msg}"
    if not isinstance(payload, dict):
        return None, "Malformed machine-readable output: expected an object."
    return payload, None


def iter_stdout_lines(result: CommandResult) -> Iterator[str]:
    source = stdout_source(result)
    if source is None:
        return
    pending = ""
    for chunk in source:
        pending += chunk
        while "\n" in pending:
            line, pending = pending.split("\n", maxsplit=1)
            yield line.rstrip("\r")
    if pending:
        yield pending


def iter_stdout_json_objects(
    result: CommandResult,
) -> Iterator[Mapping[str, object] | None]:
    for line in iter_stdout_lines(result):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            yield None
            return
        yield value if isinstance(value, dict) else None


def stdout_source(result: CommandResult) -> Iterable[str] | None:
    return stream_source(result.stdout_text, result.stdout_path)


def stream_source(fallback_text: str, path: Path | None) -> Iterable[str] | None:
    if path is not None and path.is_file() and path_has_non_whitespace_text(path):
        return _chunks_from_file(path)
    if not fallback_text.strip():
        return None
    return (fallback_text,)


def new_owned_output_file() -> Path:
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="crewplane-machine-result-",
        suffix=".txt",
        delete=False,
    ) as handle:
        return Path(handle.name)


def remove_owned_path(path: Path | None) -> None:
    if path is not None:
        path.unlink(missing_ok=True)


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


def _chunks_from_file(path: Path) -> Iterator[str]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        while chunk := handle.read(STREAM_READ_BYTES):
            yield chunk
