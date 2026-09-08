from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from crewplane.architecture.contracts import (
    CommandResult,
    OutputExtractionResult,
)

from . import claude_json as _claude_json
from .streaming import (
    iter_stdout_json_objects,
    load_stdout_json,
    path_decoded_character_count,
    path_has_non_whitespace_text,
)

# Usage metadata is normally small; bound buffering of malformed provider output.
MAX_CAPTURED_CLAUDE_USAGE_BYTES = 1024 * 1024
ClaudeJsonDocument = _claude_json.ClaudeJsonDocument


def extract_claude_output(
    result: CommandResult,
    structured_output_file: Path | None,  # noqa: ARG001 - Required by OutputExtractor callback.
) -> OutputExtractionResult:
    return _claude_json.extract_claude_output(
        result,
        MAX_CAPTURED_CLAUDE_USAGE_BYTES,
    )


def read_claude_model_usage(result: CommandResult) -> tuple[object | None, str | None]:
    return _claude_json.read_claude_model_usage(
        result,
        MAX_CAPTURED_CLAUDE_USAGE_BYTES,
    )


def extract_codex_output(
    result: CommandResult,  # noqa: ARG001 - Required by OutputExtractor callback.
    structured_output_file: Path | None,
) -> OutputExtractionResult:
    if structured_output_file is None or not structured_output_file.exists():
        return _missing_output()
    if not path_has_non_whitespace_text(structured_output_file):
        return _missing_output()
    return OutputExtractionResult(
        output_text="",
        output_extraction_status="success",
        output_path=structured_output_file,
        output_char_count=path_decoded_character_count(structured_output_file),
    )


def extract_gemini_output(
    result: CommandResult,
    structured_output_file: Path | None,  # noqa: ARG001 - Required by OutputExtractor callback.
) -> OutputExtractionResult:
    payload, error = load_stdout_json(result)
    if error is not None:
        return _malformed_output()
    if payload is None or "response" not in payload:
        return _missing_output()
    response = payload["response"]
    if not isinstance(response, str):
        return _malformed_output()
    if not response.strip():
        return _missing_output()
    return OutputExtractionResult(
        output_text=response,
        output_extraction_status="success",
        output_char_count=len(response),
    )


def extract_kilo_output(
    result: CommandResult,
    structured_output_file: Path | None,  # noqa: ARG001 - Required by OutputExtractor callback.
) -> OutputExtractionResult:
    text_parts: list[str] = []
    for event in iter_stdout_json_objects(result):
        if event is None:
            return _malformed_output()
        text = _kilo_text_event(event)
        if text is not None:
            text = text.strip()
            if text:
                text_parts.append(text)
    if not text_parts:
        return _missing_output()
    output_text = "\n".join(text_parts) + "\n"
    return OutputExtractionResult(
        output_text=output_text,
        output_extraction_status="success",
        output_char_count=len(output_text),
    )


def _kilo_text_event(event: Mapping[str, object]) -> str | None:
    if event.get("type") != "text":
        return None
    part = event.get("part")
    text = part.get("text") if isinstance(part, dict) else event.get("text")
    return text if isinstance(text, str) else None


def _missing_output() -> OutputExtractionResult:
    return OutputExtractionResult(output_text="", output_extraction_status="missing")


def _malformed_output() -> OutputExtractionResult:
    return OutputExtractionResult(
        output_text="",
        output_extraction_status="malformed",
    )
