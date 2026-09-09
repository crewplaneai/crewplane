from __future__ import annotations

__all__ = [
    "ClaudeJsonDocument",
    "extract_claude_output",
    "read_claude_model_usage",
]

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from crewplane.architecture.contracts import (
    CommandResult,
    OutputExtractionResult,
)
from crewplane.core.file_text import path_has_non_whitespace_text

from .claude_json_parser import (
    ClaudeJsonParseError,
    parse_claude_model_usage,
    parse_claude_result,
)
from .streaming import (
    new_owned_output_file,
    remove_owned_path,
    stdout_source,
    stream_source,
)


@dataclass(frozen=True)
class ClaudeJsonDocument:
    """Incrementally parsed Claude result and optional model-usage payload."""

    result_path: Path | None
    result_char_count: int
    model_usage: object | None
    error: str | None = None


def extract_claude_output(
    result: CommandResult,
    max_captured_usage_bytes: int,  # noqa: ARG001 - Stable parser facade contract.
) -> OutputExtractionResult:
    """Extract Claude's result string into an owned temporary output file."""
    extraction = _extract_claude_document(result)
    if extraction.error is not None:
        return _malformed_output()
    if extraction.result_path is None:
        return _missing_output()
    if not _path_has_output(extraction.result_path):
        remove_owned_path(extraction.result_path)
        return _missing_output()
    return OutputExtractionResult(
        output_text="",
        output_extraction_status="success",
        output_path=extraction.result_path,
        output_char_count=extraction.result_char_count,
        owns_output_path=True,
    )


def _path_has_output(path: Path) -> bool:
    try:
        return path_has_non_whitespace_text(path)
    except BaseException:
        remove_owned_path(path)
        raise


def _extract_claude_document(result: CommandResult) -> ClaudeJsonDocument:
    document = _parse_result_source(stdout_source(result))
    if document.error is not None or document.result_path is not None:
        return document
    stderr_source = stream_source(result.stderr_text, result.stderr_path)
    if stderr_source is None:
        return document
    return _parse_result_source(stderr_source)


def _parse_result_source(source: Iterable[str] | None) -> ClaudeJsonDocument:
    if source is None:
        return ClaudeJsonDocument(None, 0, None)
    output_path = new_owned_output_file()
    retain_output = False
    try:
        char_count = parse_claude_result(source, output_path)
        if char_count is None:
            return ClaudeJsonDocument(None, 0, None)
        document = ClaudeJsonDocument(output_path, char_count, None)
        retain_output = True
        return document
    except ClaudeJsonParseError:
        return _malformed_document()
    finally:
        if not retain_output:
            remove_owned_path(output_path)


def read_claude_model_usage(
    result: CommandResult,
    max_captured_usage_bytes: int,
) -> tuple[object | None, str | None]:
    """Read the bounded modelUsage payload, returning parse errors as data."""
    source = stdout_source(result)
    if source is None:
        source = stream_source(result.stderr_text, result.stderr_path)
    if source is None:
        return None, None
    try:
        return parse_claude_model_usage(source, max_captured_usage_bytes), None
    except ClaudeJsonParseError:
        return None, "Malformed Claude JSON output."


def _malformed_document() -> ClaudeJsonDocument:
    return ClaudeJsonDocument(None, 0, None, "Malformed Claude JSON output.")


def _missing_output() -> OutputExtractionResult:
    return OutputExtractionResult(output_text="", output_extraction_status="missing")


def _malformed_output() -> OutputExtractionResult:
    return OutputExtractionResult(
        output_text="",
        output_extraction_status="malformed",
    )
