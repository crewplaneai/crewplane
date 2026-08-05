from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from crewplane.architecture.contracts import (
    CommandResult,
    OutputExtractionResult,
)

from .streaming import (
    iter_stdout_json_objects,
    load_stdout_json,
    new_owned_output_file,
    path_decoded_character_count,
    path_has_non_whitespace_text,
    remove_owned_path,
    stdout_source,
    stream_source,
)

# Usage metadata is normally small; bound buffering of malformed provider output.
MAX_CAPTURED_CLAUDE_USAGE_BYTES = 1024 * 1024
_SIMPLE_JSON_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


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


def extract_claude_output(
    result: CommandResult,
    structured_output_file: Path | None,  # noqa: ARG001 - Required by OutputExtractor callback.
) -> OutputExtractionResult:
    extraction = _extract_claude_document(
        result,
        use_stderr_fallback=True,
        capture_result=True,
        parse_result=True,
        parse_model_usage=False,
    )
    if extraction.error is not None:
        return _malformed_output()
    if extraction.result_path is None:
        return _missing_output()
    if not path_has_non_whitespace_text(extraction.result_path):
        remove_owned_path(extraction.result_path)
        return _missing_output()
    return OutputExtractionResult(
        output_text="",
        output_extraction_status="success",
        output_path=extraction.result_path,
        output_char_count=extraction.result_char_count,
        owns_output_path=True,
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
    malformed_error: str | None = None
    for event in iter_stdout_json_objects(result):
        if event is None:
            malformed_error = "Malformed Kilo JSON output."
            break
        text = _kilo_text_event(event)
        if text is not None:
            text = text.strip()
            if text:
                text_parts.append(text)
    if malformed_error is not None:
        return _malformed_output()
    if not text_parts:
        return _missing_output()
    output_text = "\n".join(text_parts) + "\n"
    return OutputExtractionResult(
        output_text=output_text,
        output_extraction_status="success",
        output_char_count=len(output_text),
    )


@dataclass(frozen=True)
class ClaudeJsonDocument:
    result_path: Path | None
    result_char_count: int
    model_usage: object | None
    error: str | None = None


def _extract_claude_document(
    result: CommandResult,
    use_stderr_fallback: bool,
    capture_result: bool,
    parse_result: bool,
    parse_model_usage: bool,
) -> ClaudeJsonDocument:
    document = _parse_claude_source(
        stdout_source(result),
        capture_result=capture_result,
        parse_result=parse_result,
        parse_model_usage=parse_model_usage,
    )
    if use_stderr_fallback and document.error is None and document.result_path is None:
        stderr_source = stream_source(result.stderr_text, result.stderr_path)
        if stderr_source is not None:
            document = _parse_claude_source(
                stderr_source,
                capture_result=capture_result,
                parse_result=parse_result,
                parse_model_usage=parse_model_usage,
            )
    return document


def read_claude_model_usage(result: CommandResult) -> tuple[object | None, str | None]:
    source = stdout_source(result)
    if source is None:
        source = stream_source(result.stderr_text, result.stderr_path)
    document = _parse_claude_source(
        source,
        capture_result=False,
        parse_result=False,
        parse_model_usage=True,
    )
    return document.model_usage, document.error


def _parse_claude_source(
    source: Iterable[str] | None,
    capture_result: bool,
    parse_result: bool,
    parse_model_usage: bool,
) -> ClaudeJsonDocument:
    if source is None:
        return ClaudeJsonDocument(None, 0, None)
    output_path = new_owned_output_file() if capture_result else None
    parser = _ClaudeJsonParser(
        source,
        output_path,
        parse_result=parse_result,
        parse_model_usage=parse_model_usage,
    )
    try:
        parser.parse()
    except _ClaudeJsonParseError:
        remove_owned_path(output_path)
        return ClaudeJsonDocument(None, 0, None, "Malformed Claude JSON output.")
    if output_path is not None and not parser.result_seen:
        remove_owned_path(output_path)
        output_path = None
    return ClaudeJsonDocument(
        result_path=output_path,
        result_char_count=parser.result_char_count,
        model_usage=parser.model_usage,
        error=None,
    )


class _ClaudeJsonParseError(ValueError):
    pass


class _JsonCharCursor:
    def __init__(self, chunks: Iterable[str]) -> None:
        self._chunks = iter(chunks)
        self._current = ""
        self._index = 0
        self._pushback: list[str] = []

    def read(self) -> str | None:
        if self._pushback:
            return self._pushback.pop()
        while self._index >= len(self._current):
            self._current = next(self._chunks, "")
            self._index = 0
            if not self._current:
                return None
        char = self._current[self._index]
        self._index += 1
        return char

    def push(self, chars: Iterable[str]) -> None:
        self._pushback.extend(reversed(tuple(chars)))


class _ClaudeJsonParser:
    def __init__(
        self,
        chunks: Iterable[str],
        output_path: Path | None,
        parse_result: bool,
        parse_model_usage: bool,
    ) -> None:
        self._cursor = _JsonCharCursor(chunks)
        self._output_path = output_path
        self._parse_result = parse_result
        self._parse_model_usage = parse_model_usage
        self.result_seen = False
        self.result_char_count = 0
        self.model_usage: object | None = None
        self._capture_overflow = False
        self._captured_size = 0

    def parse(self) -> ClaudeJsonDocument:
        self._skip_whitespace()
        self._expect("{")
        self._skip_whitespace()
        if self._consume_object_end():
            return ClaudeJsonDocument(None, 0, None)
        while True:
            key = self._read_string()
            self._skip_whitespace()
            self._expect(":")
            self._parse_member_value(key)
            self._skip_whitespace()
            separator = self._read_required()
            if separator == "}":
                break
            if separator != ",":
                raise _ClaudeJsonParseError("Expected object separator.")
            self._skip_whitespace()
        self._skip_trailing_whitespace()
        if self._capture_overflow:
            raise _ClaudeJsonParseError("Captured Claude usage payload is too large.")
        return ClaudeJsonDocument(
            result_path=self._output_path if self.result_seen else None,
            result_char_count=self.result_char_count,
            model_usage=self.model_usage,
        )

    def _parse_member_value(self, key: str) -> None:
        self._skip_whitespace()
        if key == "result" and self._parse_result:
            self._read_result_value()
            return
        if key == "modelUsage" and self._parse_model_usage:
            self.model_usage = self._read_captured_value()
            return
        self._skip_value()

    def _read_result_value(self) -> None:
        if self._peek() != '"':
            self._skip_value()
            raise _ClaudeJsonParseError("Claude result must be a JSON string.")
        self.result_seen = True
        if self._output_path is None:
            self._stream_string(None)
            return
        with self._output_path.open("w", encoding="utf-8") as handle:
            self.result_char_count = self._stream_string(handle)

    def _read_captured_value(self) -> object | None:
        captured: list[str] = []
        self._skip_value(captured)
        try:
            return json.loads("".join(captured))
        except json.JSONDecodeError as exc:
            raise _ClaudeJsonParseError("Malformed Claude modelUsage payload.") from exc

    def _skip_value(self, captured: list[str] | None = None) -> None:
        self._skip_whitespace(captured)
        char = self._peek()
        if char is None:
            raise _ClaudeJsonParseError("Unexpected end of JSON value.")
        if char == '"':
            self._skip_string(captured)
            return
        if char == "{":
            self._skip_bracketed_value("{", "}", captured)
            return
        if char == "[":
            self._skip_bracketed_value("[", "]", captured)
            return
        self._skip_scalar(captured)

    def _skip_bracketed_value(
        self,
        opener: str,
        closer: str,
        captured: list[str] | None,
    ) -> None:
        self._expect(opener, captured)
        stack = [closer]
        while stack:
            char = self._read_required()
            self._capture(captured, char)
            if char == '"':
                self._skip_string_tail(captured)
            elif char == "{":
                stack.append("}")
            elif char == "[":
                stack.append("]")
            elif char == stack[-1]:
                stack.pop()

    def _skip_scalar(self, captured: list[str] | None) -> None:
        while True:
            char = self._read_required()
            if char in {",", "}", "]"}:
                self._cursor.push((char,))
                return
            self._capture(captured, char)

    def _skip_string(self, captured: list[str] | None) -> None:
        self._expect('"', captured)
        self._skip_string_tail(captured)

    def _skip_string_tail(self, captured: list[str] | None) -> None:
        while True:
            char = self._read_required()
            self._capture(captured, char)
            if char == '"':
                return
            if char != "\\":
                continue
            escaped = self._read_required()
            self._capture(captured, escaped)
            if escaped == "u":
                self._capture(captured, self._read_required())
                self._capture(captured, self._read_required())
                self._capture(captured, self._read_required())
                self._capture(captured, self._read_required())

    def _read_string(self) -> str:
        self._expect('"')
        chars: list[str] = []
        while True:
            char = self._read_required()
            if char == '"':
                return "".join(chars)
            if char == "\\":
                char = self._read_escape()
            chars.append(char)

    def _stream_string(self, sink: TextIO | None) -> int:
        self._expect('"')
        count = 0
        while True:
            char = self._read_required()
            if char == '"':
                return count
            if char == "\\":
                char = self._read_escape()
            if sink is not None:
                sink.write(char)
            count += len(char)

    def _read_escape(self) -> str:
        escaped = self._read_required()
        if escaped == "u":
            return self._read_unicode_escape()

        replacement = _SIMPLE_JSON_ESCAPES.get(escaped)
        if replacement is None:
            raise _ClaudeJsonParseError("Invalid JSON string escape.")
        return replacement

    def _read_unicode_escape(self) -> str:
        value = self._read_hex_codepoint()
        if 0xD800 <= value <= 0xDBFF:
            next_chars = [self._read_required(), self._read_required()]
            if next_chars == ["\\", "u"]:
                low = self._read_hex_codepoint()
                if 0xDC00 <= low <= 0xDFFF:
                    combined = 0x10000 + ((value - 0xD800) << 10) + (low - 0xDC00)
                    return chr(combined)
            self._cursor.push(next_chars)
            return "\ufffd"
        if 0xDC00 <= value <= 0xDFFF:
            return "\ufffd"
        return chr(value)

    def _read_hex_codepoint(self) -> int:
        chars = [self._read_required() for _ in range(4)]
        if any(char not in "0123456789abcdefABCDEF" for char in chars):
            raise _ClaudeJsonParseError("Invalid unicode escape.")
        return int("".join(chars), 16)

    def _consume_object_end(self) -> bool:
        if self._peek() != "}":
            return False
        self._expect("}")
        self._skip_trailing_whitespace()
        return True

    def _skip_whitespace(self, captured: list[str] | None = None) -> None:
        while True:
            char = self._read_required()
            if not char.isspace():
                self._cursor.push((char,))
                return
            self._capture(captured, char)

    def _skip_trailing_whitespace(self) -> None:
        while True:
            char = self._cursor.read()
            if char is None:
                return
            if not char.isspace():
                raise _ClaudeJsonParseError("Unexpected trailing JSON data.")

    def _peek(self) -> str | None:
        char = self._cursor.read()
        if char is not None:
            self._cursor.push((char,))
        return char

    def _expect(self, expected: str, captured: list[str] | None = None) -> None:
        char = self._read_required()
        if char != expected:
            raise _ClaudeJsonParseError(f"Expected {expected!r}.")
        self._capture(captured, char)

    def _read_required(self) -> str:
        char = self._cursor.read()
        if char is None:
            raise _ClaudeJsonParseError("Unexpected end of JSON input.")
        return char

    def _capture(self, captured: list[str] | None, char: str) -> None:
        if captured is None:
            return
        char_bytes = len(char.encode("utf-8"))
        if self._captured_size + char_bytes > MAX_CAPTURED_USAGE_BYTES:
            self._capture_overflow = True
            return
        captured.append(char)
        self._captured_size += char_bytes


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
