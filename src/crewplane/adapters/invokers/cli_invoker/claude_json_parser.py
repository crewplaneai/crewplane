from __future__ import annotations

__all__ = [
    "ClaudeJsonParseError",
    "parse_claude_model_usage",
    "parse_claude_result",
]

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import Enum, StrEnum, auto
from pathlib import Path
from typing import Literal, TextIO

from .json_number import (
    JSON_NUMBER_TERMINAL_STATES,
    JsonNumberError,
    JsonNumberState,
    advance_json_number,
    start_json_number,
)


class ClaudeJsonParseError(ValueError):
    """Raised when Claude output is malformed for the requested parse mode."""


def parse_claude_result(chunks: Iterable[str], output_path: Path) -> int | None:
    """Stream Claude's result field and return its decoded character count."""
    selection = _ResultSelection(output_path)
    _ClaudeJsonParser(chunks, selection).parse()
    return selection.char_count


def parse_claude_model_usage(
    chunks: Iterable[str],
    max_captured_bytes: int,
) -> object | None:
    """Decode Claude's bounded modelUsage field when present."""
    selection = _ModelUsageSelection(max_captured_bytes)
    _ClaudeJsonParser(chunks, selection).parse()
    return selection.value


type _CaptureBuffer = list[str] | None

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
_JSON_SCALAR_END = _JSON_WHITESPACE | frozenset(",}]")
_JSON_LITERAL_SUFFIXES = {"f": "alse", "n": "ull", "t": "rue"}


class _JsonContainer(StrEnum):
    OBJECT = "{"
    ARRAY = "["

    @property
    def closer(self) -> Literal["}", "]"]:
        return "}" if self is _JsonContainer.OBJECT else "]"


class _JsonContainerPhase(Enum):
    ITEM_OR_END = auto()
    ITEM = auto()
    SEPARATOR = auto()


@dataclass(slots=True)
class _JsonContainerFrame:
    container: _JsonContainer
    phase: _JsonContainerPhase = _JsonContainerPhase.ITEM_OR_END


@dataclass(slots=True)
class _ResultSelection:
    output_path: Path
    char_count: int | None = None


@dataclass(slots=True)
class _ModelUsageSelection:
    max_captured_bytes: int
    value: object | None = None


type _ClaudeJsonSelection = _ResultSelection | _ModelUsageSelection


class _JsonCharCursor:
    __slots__ = ("_chunks", "_current", "_index", "_pushback")

    def __init__(self, chunks: Iterable[str]) -> None:
        self._chunks = iter(chunks)
        self._current = ""
        self._index = 0
        self._pushback: list[str] = []

    def read(self) -> str | None:
        if self._pushback:
            return self._pushback.pop()
        while self._index >= len(self._current):
            chunk = next(self._chunks, None)
            if chunk is None:
                return None
            if not chunk:
                continue
            self._current = chunk
            self._index = 0
        char = self._current[self._index]
        self._index += 1
        return char

    def peek(self) -> str | None:
        char = self.read()
        if char is not None:
            self.unread((char,))
        return char

    def unread(self, chars: Iterable[str]) -> None:
        self._pushback.extend(reversed(tuple(chars)))


class _ClaudeJsonParser:
    def __init__(
        self,
        chunks: Iterable[str],
        selection: _ClaudeJsonSelection,
    ) -> None:
        self._cursor = _JsonCharCursor(chunks)
        self._selection = selection
        self._max_captured_bytes = (
            selection.max_captured_bytes
            if isinstance(selection, _ModelUsageSelection)
            else 0
        )
        self._captured_size = 0

    def parse(self) -> None:
        self._skip_whitespace()
        self._expect("{")
        self._parse_members()
        self._skip_trailing_whitespace()

    def _parse_members(self) -> None:
        self._skip_whitespace()
        if self._consume_object_end():
            return
        while True:
            self._parse_member()
            self._skip_whitespace()
            if not self._read_has_next_item("}", None):
                return
            self._skip_whitespace()

    def _parse_member(self) -> None:
        key = self._read_string()
        self._skip_whitespace()
        self._expect(":")
        self._parse_member_value(key)

    def _parse_member_value(self, key: str) -> None:
        self._skip_whitespace()
        if key == "result" and isinstance(self._selection, _ResultSelection):
            self._selection.char_count = self._read_result_value(
                self._selection.output_path
            )
            return
        if key == "modelUsage" and isinstance(self._selection, _ModelUsageSelection):
            self._selection.value = self._read_captured_value()
            return
        self._skip_value()

    def _read_result_value(self, output_path: Path) -> int:
        if self._cursor.peek() != '"':
            self._skip_value()
            raise ClaudeJsonParseError("Claude result must be a JSON string.")
        with output_path.open("w", encoding="utf-8") as handle:
            return self._stream_string(handle)

    def _read_captured_value(self) -> object | None:
        captured: list[str] = []
        self._skip_value(captured)
        try:
            decoded: object = json.loads("".join(captured))
        except (ValueError, RecursionError) as exc:
            raise ClaudeJsonParseError("Malformed Claude modelUsage payload.") from exc
        return decoded

    def _skip_value(self, captured: _CaptureBuffer = None) -> None:
        container = self._consume_value_start(captured)
        if container is not None:
            self._skip_container_tail(container, captured)

    def _consume_value_start(
        self,
        captured: _CaptureBuffer,
    ) -> _JsonContainer | None:
        self._skip_whitespace(captured)
        char = self._cursor.peek()
        if char is None:
            raise ClaudeJsonParseError("Unexpected end of JSON value.")
        if char == '"':
            self._skip_string(captured)
            return None
        if char == _JsonContainer.OBJECT:
            self._expect(_JsonContainer.OBJECT, captured)
            return _JsonContainer.OBJECT
        if char == _JsonContainer.ARRAY:
            self._expect(_JsonContainer.ARRAY, captured)
            return _JsonContainer.ARRAY
        self._skip_scalar(captured)
        return None

    def _skip_container_tail(
        self,
        container: _JsonContainer,
        captured: _CaptureBuffer,
    ) -> None:
        frames = [_JsonContainerFrame(container)]
        while frames:
            self._advance_container(frames, captured)

    def _advance_container(
        self,
        frames: list[_JsonContainerFrame],
        captured: _CaptureBuffer,
    ) -> None:
        frame = frames[-1]
        self._skip_whitespace(captured)
        if self._consume_empty_container(frame, captured):
            frames.pop()
            return
        if frame.phase is not _JsonContainerPhase.SEPARATOR:
            nested = self._skip_container_item(frame, captured)
            frame.phase = _JsonContainerPhase.SEPARATOR
            if nested is not None:
                frames.append(_JsonContainerFrame(nested))
            return
        if self._read_has_next_item(frame.container.closer, captured):
            frame.phase = _JsonContainerPhase.ITEM
        else:
            frames.pop()

    def _consume_empty_container(
        self,
        frame: _JsonContainerFrame,
        captured: _CaptureBuffer,
    ) -> bool:
        if (
            frame.phase is not _JsonContainerPhase.ITEM_OR_END
            or self._cursor.peek() != frame.container.closer
        ):
            return False
        self._expect(frame.container.closer, captured)
        return True

    def _skip_container_item(
        self,
        frame: _JsonContainerFrame,
        captured: _CaptureBuffer,
    ) -> _JsonContainer | None:
        if frame.container is _JsonContainer.OBJECT:
            self._skip_string(captured)
            self._skip_whitespace(captured)
            self._expect(":", captured)
        return self._consume_value_start(captured)

    def _read_has_next_item(
        self,
        closer: str,
        captured: _CaptureBuffer,
    ) -> bool:
        separator = self._read_required(captured)
        if separator == closer:
            return False
        if separator != ",":
            raise ClaudeJsonParseError("Expected JSON value separator.")
        return True

    def _skip_scalar(self, captured: _CaptureBuffer) -> None:
        first = self._read_required(captured)
        literal_suffix = _JSON_LITERAL_SUFFIXES.get(first)
        if literal_suffix is not None:
            self._skip_literal_suffix(literal_suffix, captured)
            return
        self._skip_number(first, captured)

    def _skip_literal_suffix(
        self,
        suffix: str,
        captured: _CaptureBuffer,
    ) -> None:
        for expected in suffix:
            if self._read_required(captured) != expected:
                raise ClaudeJsonParseError("Invalid JSON scalar value.")
        self._require_scalar_end()

    def _skip_number(self, first: str, captured: _CaptureBuffer) -> None:
        try:
            state = start_json_number(first)
            state = self._consume_number_tail(state, captured)
        except JsonNumberError as exc:
            raise ClaudeJsonParseError("Invalid JSON scalar value.") from exc
        if state not in JSON_NUMBER_TERMINAL_STATES:
            raise ClaudeJsonParseError("Invalid JSON scalar value.")

    def _consume_number_tail(
        self,
        state: JsonNumberState,
        captured: _CaptureBuffer,
    ) -> JsonNumberState:
        while True:
            char = self._cursor.read()
            if char is None:
                return state
            if char in _JSON_SCALAR_END:
                self._cursor.unread((char,))
                return state
            self._capture(captured, char)
            state = advance_json_number(state, char)

    def _require_scalar_end(self) -> None:
        char = self._cursor.peek()
        if char is not None and char not in _JSON_SCALAR_END:
            raise ClaudeJsonParseError("Invalid JSON scalar value.")

    def _skip_string(self, captured: _CaptureBuffer) -> None:
        self._expect('"', captured)
        while True:
            char = self._read_required(captured)
            if char == '"':
                return
            self._validate_string_char(char)
            if char == "\\":
                self._skip_raw_escape(captured)

    def _skip_raw_escape(self, captured: _CaptureBuffer) -> None:
        escaped = self._read_required(captured)
        if escaped in _SIMPLE_JSON_ESCAPES:
            return
        if escaped != "u":
            raise ClaudeJsonParseError("Invalid JSON string escape.")
        self._read_hex_quad(captured)

    def _read_string(self) -> str:
        return "".join(self._iter_decoded_string_chars())

    def _stream_string(self, sink: TextIO) -> int:
        count = 0
        for char in self._iter_decoded_string_chars():
            sink.write(char)
            count += len(char)
        return count

    def _iter_decoded_string_chars(self) -> Iterator[str]:
        self._expect('"')
        while True:
            char = self._read_required()
            if char == '"':
                return
            self._validate_string_char(char)
            yield self._read_escape() if char == "\\" else char

    @staticmethod
    def _validate_string_char(char: str) -> None:
        if ord(char) < 0x20:
            raise ClaudeJsonParseError("Unescaped control character.")

    def _read_escape(self) -> str:
        escaped = self._read_required()
        if escaped == "u":
            return self._read_unicode_escape()
        replacement = _SIMPLE_JSON_ESCAPES.get(escaped)
        if replacement is None:
            raise ClaudeJsonParseError("Invalid JSON string escape.")
        return replacement

    def _read_unicode_escape(self) -> str:
        value = int(self._read_hex_quad(), 16)
        if 0xD800 <= value <= 0xDBFF:
            return self._read_surrogate_pair(value)
        if 0xDC00 <= value <= 0xDFFF:
            return "\ufffd"
        return chr(value)

    def _read_surrogate_pair(self, high: int) -> str:
        prefix = [self._read_required(), self._read_required()]
        if prefix != ["\\", "u"]:
            self._cursor.unread(prefix)
            return "\ufffd"
        low_chars = self._read_hex_quad()
        low = int(low_chars, 16)
        if 0xDC00 <= low <= 0xDFFF:
            combined = 0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00)
            return chr(combined)
        self._cursor.unread((*prefix, *low_chars))
        return "\ufffd"

    def _read_hex_quad(self, captured: _CaptureBuffer = None) -> str:
        chars = [self._read_required(captured) for _ in range(4)]
        if any(char not in "0123456789abcdefABCDEF" for char in chars):
            raise ClaudeJsonParseError("Invalid unicode escape.")
        return "".join(chars)

    def _consume_object_end(self) -> bool:
        if self._cursor.peek() != "}":
            return False
        self._expect("}")
        return True

    def _skip_whitespace(self, captured: _CaptureBuffer = None) -> None:
        while True:
            char = self._read_required()
            if char not in _JSON_WHITESPACE:
                self._cursor.unread((char,))
                return
            self._capture(captured, char)

    def _skip_trailing_whitespace(self) -> None:
        while True:
            char = self._cursor.read()
            if char is None:
                return
            if char not in _JSON_WHITESPACE:
                raise ClaudeJsonParseError("Unexpected trailing JSON data.")

    def _expect(self, expected: str, captured: _CaptureBuffer = None) -> None:
        char = self._read_required()
        if char != expected:
            raise ClaudeJsonParseError(f"Expected {expected!r}.")
        self._capture(captured, char)

    def _read_required(self, captured: _CaptureBuffer = None) -> str:
        char = self._cursor.read()
        if char is None:
            raise ClaudeJsonParseError("Unexpected end of JSON input.")
        self._capture(captured, char)
        return char

    def _capture(self, captured: _CaptureBuffer, char: str) -> None:
        if captured is None:
            return
        char_bytes = len(char.encode("utf-8"))
        if self._captured_size + char_bytes > self._max_captured_bytes:
            raise ClaudeJsonParseError("Captured Claude usage payload is too large.")
        captured.append(char)
        self._captured_size += char_bytes
