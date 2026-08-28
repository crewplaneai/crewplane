from __future__ import annotations

__all__ = [
    "ClaudeJsonDocument",
    "extract_claude_output",
    "read_claude_model_usage",
]

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from crewplane.architecture.contracts import (
    CommandResult,
    OutputExtractionResult,
)

from .streaming import (
    new_owned_output_file,
    path_has_non_whitespace_text,
    remove_owned_path,
    stdout_source,
    stream_source,
)

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
_JSON_WHITESPACE = frozenset(" \t\r\n")
_JSON_LITERAL_SUFFIXES = {"f": "alse", "n": "ull", "t": "rue"}
_JSON_NUMBER_TERMINAL_STATES = frozenset({"zero", "integer", "fraction", "exponent"})


def extract_claude_output(
    result: CommandResult,
    max_captured_usage_bytes: int,
) -> OutputExtractionResult:
    """Extract Claude's result string into an owned temporary output file."""
    extraction = _extract_claude_document(
        result,
        max_captured_usage_bytes=max_captured_usage_bytes,
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


@dataclass(frozen=True)
class ClaudeJsonDocument:
    """Incrementally parsed Claude result and optional model-usage payload."""

    result_path: Path | None
    result_char_count: int
    model_usage: object | None
    error: str | None = None


def _extract_claude_document(
    result: CommandResult,
    max_captured_usage_bytes: int,
) -> ClaudeJsonDocument:
    document = _parse_claude_source(
        stdout_source(result),
        capture_result=True,
        parse_result=True,
        parse_model_usage=False,
        max_captured_usage_bytes=max_captured_usage_bytes,
    )
    if document.error is None and document.result_path is None:
        stderr_source = stream_source(result.stderr_text, result.stderr_path)
        if stderr_source is not None:
            document = _parse_claude_source(
                stderr_source,
                capture_result=True,
                parse_result=True,
                parse_model_usage=False,
                max_captured_usage_bytes=max_captured_usage_bytes,
            )
    return document


def read_claude_model_usage(
    result: CommandResult,
    max_captured_usage_bytes: int,
) -> tuple[object | None, str | None]:
    """Read the bounded modelUsage payload, returning parse errors as data."""
    source = stdout_source(result)
    if source is None:
        source = stream_source(result.stderr_text, result.stderr_path)
    document = _parse_claude_source(
        source,
        capture_result=False,
        parse_result=False,
        parse_model_usage=True,
        max_captured_usage_bytes=max_captured_usage_bytes,
    )
    return document.model_usage, document.error


def _parse_claude_source(
    source: Iterable[str] | None,
    capture_result: bool,
    parse_result: bool,
    parse_model_usage: bool,
    max_captured_usage_bytes: int,
) -> ClaudeJsonDocument:
    if source is None:
        return ClaudeJsonDocument(None, 0, None)
    output_path = new_owned_output_file() if capture_result else None
    parser = _ClaudeJsonParser(
        source,
        output_path,
        parse_result=parse_result,
        parse_model_usage=parse_model_usage,
        max_captured_usage_bytes=max_captured_usage_bytes,
    )
    try:
        document = parser.parse()
    except _ClaudeJsonParseError:
        remove_owned_path(output_path)
        return ClaudeJsonDocument(None, 0, None, "Malformed Claude JSON output.")
    if output_path is not None and document.result_path is None:
        remove_owned_path(output_path)
    return document


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
        max_captured_usage_bytes: int,
    ) -> None:
        self._cursor = _JsonCharCursor(chunks)
        self._output_path = output_path
        self._parse_result = parse_result
        self._parse_model_usage = parse_model_usage
        self._max_captured_usage_bytes = max_captured_usage_bytes
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
            decoded: object = json.loads("".join(captured))
        except json.JSONDecodeError as exc:
            raise _ClaudeJsonParseError("Malformed Claude modelUsage payload.") from exc
        return decoded

    def _skip_value(self, captured: list[str] | None = None) -> None:
        self._skip_whitespace(captured)
        char = self._peek()
        if char is None:
            raise _ClaudeJsonParseError("Unexpected end of JSON value.")
        if char == '"':
            self._skip_string(captured)
            return
        if char == "{":
            self._skip_bracketed_value("{", captured)
            return
        if char == "[":
            self._skip_bracketed_value("[", captured)
            return
        self._skip_scalar(captured)

    def _skip_bracketed_value(
        self,
        opener: str,
        captured: list[str] | None,
    ) -> None:
        self._expect(opener, captured)
        stack = [(opener, "first")]
        while stack:
            container, state = stack[-1]
            self._skip_whitespace(captured)
            expected_closer = "}" if container == "{" else "]"
            if state == "first" and self._peek() == expected_closer:
                self._expect(expected_closer, captured)
                stack.pop()
                continue
            if container == "{" and state in {"first", "item"}:
                self._skip_string(captured)
                stack[-1] = (container, "colon")
                continue
            if container == "{" and state == "colon":
                self._expect(":", captured)
                stack[-1] = (container, "value")
                continue
            if state in {"first", "item", "value"}:
                nested_opener = self._skip_value_start(captured)
                stack[-1] = (container, "separator")
                if nested_opener is not None:
                    stack.append((nested_opener, "first"))
                continue
            separator = self._read_required()
            self._capture(captured, separator)
            if separator == expected_closer:
                stack.pop()
                continue
            if separator != ",":
                raise _ClaudeJsonParseError("Expected JSON value separator.")
            stack[-1] = (container, "item")

    def _skip_value_start(self, captured: list[str] | None) -> str | None:
        self._skip_whitespace(captured)
        char = self._peek()
        if char is None:
            raise _ClaudeJsonParseError("Unexpected end of JSON value.")
        if char == '"':
            self._skip_string(captured)
            return None
        if char in {"{", "["}:
            self._expect(char, captured)
            return char
        self._skip_scalar(captured)
        return None

    def _skip_scalar(self, captured: list[str] | None) -> None:
        first = self._read_required()
        self._capture(captured, first)
        literal_suffix = _JSON_LITERAL_SUFFIXES.get(first)
        if literal_suffix is not None:
            self._skip_literal_suffix(literal_suffix, captured)
            return
        self._skip_number(first, captured)

    def _skip_literal_suffix(
        self,
        suffix: str,
        captured: list[str] | None,
    ) -> None:
        for expected in suffix:
            char = self._read_required()
            self._capture(captured, char)
            if char != expected:
                raise _ClaudeJsonParseError("Invalid JSON scalar value.")
        self._require_scalar_end()

    def _skip_number(self, first: str, captured: list[str] | None) -> None:
        state = self._initial_number_state(first)
        while True:
            char = self._cursor.read()
            if char is None:
                break
            if char in _JSON_WHITESPACE or char in {",", "}", "]"}:
                self._cursor.push((char,))
                break
            self._capture(captured, char)
            state = self._next_number_state(state, char)
        if state not in _JSON_NUMBER_TERMINAL_STATES:
            raise _ClaudeJsonParseError("Invalid JSON scalar value.")

    @staticmethod
    def _initial_number_state(first: str) -> str:
        if first == "-":
            return "sign"
        if first == "0":
            return "zero"
        if first in "123456789":
            return "integer"
        raise _ClaudeJsonParseError("Invalid JSON scalar value.")

    @staticmethod
    def _next_number_state(state: str, char: str) -> str:
        if state == "sign":
            if char == "0":
                return "zero"
            if char in "123456789":
                return "integer"
        elif state == "zero":
            if char == ".":
                return "decimal"
            if char in "eE":
                return "exponent_start"
        elif state == "integer":
            if char.isascii() and char.isdigit():
                return "integer"
            if char == ".":
                return "decimal"
            if char in "eE":
                return "exponent_start"
        elif state == "decimal":
            if char.isascii() and char.isdigit():
                return "fraction"
        elif state == "fraction":
            if char.isascii() and char.isdigit():
                return "fraction"
            if char in "eE":
                return "exponent_start"
        elif state == "exponent_start":
            if char in "+-":
                return "exponent_sign"
            if char.isascii() and char.isdigit():
                return "exponent"
        elif (
            state in {"exponent_sign", "exponent"} and char.isascii() and char.isdigit()
        ):
            return "exponent"
        raise _ClaudeJsonParseError("Invalid JSON scalar value.")

    def _require_scalar_end(self) -> None:
        char = self._cursor.read()
        if char is None:
            return
        if char not in _JSON_WHITESPACE and char not in {",", "}", "]"}:
            raise _ClaudeJsonParseError("Invalid JSON scalar value.")
        self._cursor.push((char,))

    def _skip_string(self, captured: list[str] | None) -> None:
        self._expect('"', captured)
        self._skip_string_tail(captured)

    def _skip_string_tail(self, captured: list[str] | None) -> None:
        while True:
            char = self._read_required()
            self._capture(captured, char)
            if char == '"':
                return
            if ord(char) < 0x20:
                raise _ClaudeJsonParseError("Unescaped control character.")
            if char != "\\":
                continue
            escaped = self._read_required()
            self._capture(captured, escaped)
            if escaped in _SIMPLE_JSON_ESCAPES:
                continue
            if escaped != "u":
                raise _ClaudeJsonParseError("Invalid JSON string escape.")
            hex_chars = [self._read_required() for _ in range(4)]
            for hex_char in hex_chars:
                self._capture(captured, hex_char)
            if any(hex_char not in "0123456789abcdefABCDEF" for hex_char in hex_chars):
                raise _ClaudeJsonParseError("Invalid unicode escape.")

    def _read_string(self) -> str:
        self._expect('"')
        chars: list[str] = []
        while True:
            char = self._read_required()
            if char == '"':
                return "".join(chars)
            if ord(char) < 0x20:
                raise _ClaudeJsonParseError("Unescaped control character.")
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
            if ord(char) < 0x20:
                raise _ClaudeJsonParseError("Unescaped control character.")
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
            if char not in _JSON_WHITESPACE:
                self._cursor.push((char,))
                return
            self._capture(captured, char)

    def _skip_trailing_whitespace(self) -> None:
        while True:
            char = self._cursor.read()
            if char is None:
                return
            if char not in _JSON_WHITESPACE:
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
        if self._captured_size + char_bytes > self._max_captured_usage_bytes:
            self._capture_overflow = True
            return
        captured.append(char)
        self._captured_size += char_bytes


def _missing_output() -> OutputExtractionResult:
    return OutputExtractionResult(output_text="", output_extraction_status="missing")


def _malformed_output() -> OutputExtractionResult:
    return OutputExtractionResult(
        output_text="",
        output_extraction_status="malformed",
    )
