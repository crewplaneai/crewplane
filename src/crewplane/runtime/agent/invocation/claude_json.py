from __future__ import annotations

import codecs
import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TextIO

from crewplane.architecture.contracts import CommandResult

from ..usage import ParsedProviderUsage
from ..usage_parsing import parse_usage_mapping
from .state import ExtractedInvocationOutput

STREAM_READ_BYTES = 65_536


def extract_claude_json_result(result: CommandResult) -> ExtractedInvocationOutput:
    stdout_extraction = _extract_json_from_stream(
        result.stdout_text, result.stdout_path
    )
    if stdout_extraction.output_extraction_status != "missing":
        return stdout_extraction
    return _extract_json_from_stream(result.stderr_text, result.stderr_path)


def _extract_json_from_stream(
    fallback_text: str,
    path: Path | None,
) -> ExtractedInvocationOutput:
    if path is not None and path.is_file():
        if not _path_has_non_whitespace_text(path):
            return _missing_extraction()
        return _parse_json_stream(_chunks_from_file(path))
    if not fallback_text.strip():
        return _missing_extraction()
    return _parse_json_stream((fallback_text,))


def _missing_extraction() -> ExtractedInvocationOutput:
    return ExtractedInvocationOutput(
        output_text="",
        output_extraction_status="missing",
        parsed_provider_usage=ParsedProviderUsage(status="none"),
    )


def _chunks_from_file(path: Path) -> Iterator[str]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        while chunk := handle.read(STREAM_READ_BYTES):
            yield chunk


def _new_owned_output_file() -> Path:
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="crewplane-claude-result-",
        suffix=".txt",
        delete=False,
    ) as handle:
        return Path(handle.name)


def _parse_json_stream(chunks: Iterable[str]) -> ExtractedInvocationOutput:
    output_path = _new_owned_output_file()
    parser = _ClaudeJsonResultParser(chunks, output_path)
    try:
        parsed_usage = parser.parse()
    except _ClaudeJsonParseError:
        output_path.unlink(missing_ok=True)
        return ExtractedInvocationOutput(
            output_text="",
            output_extraction_status="malformed",
            parsed_provider_usage=ParsedProviderUsage(status="none"),
        )
    if not parser.result_seen:
        output_path.unlink(missing_ok=True)
        return ExtractedInvocationOutput(
            output_text="",
            output_extraction_status="missing",
            parsed_provider_usage=parsed_usage,
        )
    return ExtractedInvocationOutput(
        output_text="",
        output_extraction_status="success",
        parsed_provider_usage=parsed_usage,
        output_path=output_path,
        output_char_count=parser.result_char_count,
        owns_output_path=True,
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


class _ClaudeJsonResultParser:
    def __init__(self, chunks: Iterable[str], output_path: Path) -> None:
        self._cursor = _JsonCharCursor(chunks)
        self._output_path = output_path
        self.result_seen = False
        self.result_char_count = 0
        self._usage_payload: object | None = None
        self._usage_malformed = False

    def parse(self) -> ParsedProviderUsage:
        self._skip_whitespace()
        self._expect("{")
        self._skip_whitespace()
        if self._consume_object_end():
            return ParsedProviderUsage(status="none")
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
        return self._parsed_usage()

    def _parse_member_value(self, key: str) -> None:
        self._skip_whitespace()
        if key == "result":
            self._read_result_value()
            return
        if key == "usage":
            self._usage_payload = self._read_captured_value()
            return
        self._skip_value()

    def _read_result_value(self) -> None:
        if self._peek() != '"':
            self._skip_value()
            raise _ClaudeJsonParseError("Claude result must be a JSON string.")
        self.result_seen = True
        with self._output_path.open("w", encoding="utf-8") as handle:
            self.result_char_count = self._stream_string(handle)

    def _parsed_usage(self) -> ParsedProviderUsage:
        if self._usage_malformed:
            return ParsedProviderUsage(
                status="malformed",
                error="Malformed provider usage payload.",
            )
        if self._usage_payload is None:
            return ParsedProviderUsage(status="none")
        return parse_usage_mapping(self._usage_payload)

    def _read_captured_value(self) -> object | None:
        captured: list[str] = []
        self._skip_value(captured)
        try:
            return json.loads("".join(captured))
        except json.JSONDecodeError:
            self._usage_malformed = True
            return None

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
                remaining_escape_chars = 4
                while remaining_escape_chars:
                    self._capture(captured, self._read_required())
                    remaining_escape_chars -= 1

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

    def _stream_string(self, sink: TextIO) -> int:
        self._expect('"')
        count = 0
        while True:
            char = self._read_required()
            if char == '"':
                return count
            if char == "\\":
                char = self._read_escape()
            sink.write(char)
            count += len(char)

    def _read_escape(self) -> str:
        escaped = self._read_required()
        if escaped in {'"', "\\", "/"}:
            return escaped
        if escaped == "b":
            return "\b"
        if escaped == "f":
            return "\f"
        if escaped == "n":
            return "\n"
        if escaped == "r":
            return "\r"
        if escaped == "t":
            return "\t"
        if escaped == "u":
            return self._read_unicode_escape()
        raise _ClaudeJsonParseError("Invalid JSON string escape.")

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

    @staticmethod
    def _capture(captured: list[str] | None, char: str) -> None:
        if captured is not None:
            captured.append(char)


def _path_has_non_whitespace_text(path: Path) -> bool:
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    with path.open("rb") as handle:
        while chunk := handle.read(STREAM_READ_BYTES):
            if any(not char.isspace() for char in decoder.decode(chunk)):
                return True
    return any(not char.isspace() for char in decoder.decode(b"", final=True))
