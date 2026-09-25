from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from tempfile import NamedTemporaryFile

from crewplane.architecture.contracts import CommandResult, OutputExtractionResult
from crewplane.core.file_text import (
    STREAM_READ_BYTES,
    path_decoded_character_count,
    path_has_non_whitespace_text,
)


def missing_output() -> OutputExtractionResult:
    return OutputExtractionResult(output_text="", output_extraction_status="missing")


def malformed_output() -> OutputExtractionResult:
    return OutputExtractionResult(output_text="", output_extraction_status="malformed")


def extract_strict_stdout(
    result: CommandResult,
    structured_output_file: Path | None,  # noqa: ARG001 - OutputExtractor contract.
) -> OutputExtractionResult:
    """Select nonblank stdout without publishing stderr diagnostics."""
    path = result.stdout_path
    if path is not None and path.is_file():
        if not path_has_non_whitespace_text(path):
            return OutputExtractionResult("", "missing")
        return OutputExtractionResult(
            "", "success", path, path_decoded_character_count(path)
        )
    if not result.stdout_text.strip():
        return OutputExtractionResult("", "missing")
    return OutputExtractionResult(result.stdout_text, "success")


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


def _chunks_from_file(path: Path) -> Iterator[str]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        while chunk := handle.read(STREAM_READ_BYTES):
            yield chunk
