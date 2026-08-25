from __future__ import annotations

import re
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field

from .json import JsonObject, JsonValue

type JsonPathSegment = str | int
type SensitiveOptionTransform = Callable[[str, JsonValue], JsonValue]

_SENSITIVE_OPTION_PATTERN = re.compile(
    r"(secret|token|password|passwd|api[_-]?key|credential|private)",
    re.IGNORECASE,
)
_INVALID_JSON_POINTER_ESCAPE = re.compile(r"~(?:[^01]|$)")
_JSON_ARRAY_INDEX = re.compile(r"0|[1-9][0-9]*")


def transform_sensitive_integration_options(
    options: JsonObject,
    explicit_pointers: Collection[str],
    transform: SensitiveOptionTransform | None = None,
) -> tuple[JsonObject, list[str]]:
    """Discover and optionally transform sensitive values in nested options."""
    validate_sensitive_integration_option_pointers(options, explicit_pointers)
    walker = _SensitiveOptionWalker(set(explicit_pointers), transform)
    transformed = walker.walk(options, ())
    if not isinstance(transformed, dict):
        raise TypeError("Canonical integration options must remain a mapping.")
    return transformed, walker.matched_pointers


def validate_sensitive_integration_option_pointers(
    options: JsonObject,
    pointers: Collection[str],
) -> None:
    for pointer in pointers:
        segments = parse_json_pointer(pointer)
        if not _json_path_exists(options, segments):
            raise ValueError(
                "Canonical integration sensitive option JSON Pointer does not "
                f"resolve to an option: {pointer!r}"
            )


def parse_json_pointer(pointer: str) -> tuple[str, ...]:
    """Parse an RFC 6901 JSON Pointer without accepting the document root."""
    if not pointer.startswith("/"):
        raise ValueError(
            "Canonical integration sensitive option JSON Pointer must start with '/': "
            f"{pointer!r}"
        )
    return tuple(_decode_json_pointer_token(token) for token in pointer[1:].split("/"))


def json_pointer(segments: Sequence[JsonPathSegment]) -> str:
    return "".join(
        f"/{_encode_json_pointer_token(str(segment))}" for segment in segments
    )


@dataclass
class _SensitiveOptionWalker:
    explicit_pointers: set[str]
    transform: SensitiveOptionTransform | None
    matched_pointers: list[str] = field(default_factory=list)

    def walk(
        self,
        value: JsonValue,
        path: tuple[JsonPathSegment, ...],
        inherited_sensitive: bool = False,
    ) -> JsonValue:
        pointer = json_pointer(path) if path else ""
        if path and self._should_redact(
            value,
            path,
            pointer,
            inherited_sensitive,
        ):
            self.matched_pointers.append(pointer)
            if self.transform is not None:
                return self.transform(pointer, value)
            return value
        name_sensitive = bool(path) and self._is_sensitive_name(path)
        child_sensitive = inherited_sensitive or name_sensitive
        if isinstance(value, dict):
            return {
                key: self.walk(child, (*path, key), child_sensitive)
                for key, child in sorted(value.items())
            }
        if isinstance(value, list):
            return [
                self.walk(child, (*path, index), child_sensitive)
                for index, child in enumerate(value)
            ]
        return value

    def _should_redact(
        self,
        value: JsonValue,
        path: tuple[JsonPathSegment, ...],
        pointer: str,
        inherited_sensitive: bool,
    ) -> bool:
        if pointer in self.explicit_pointers:
            return True
        if inherited_sensitive and not isinstance(value, (dict, list)):
            return True
        if not self._is_sensitive_name(path):
            return False
        return not (
            isinstance(value, (dict, list)) and self._has_explicit_descendant(pointer)
        )

    @staticmethod
    def _is_sensitive_name(path: tuple[JsonPathSegment, ...]) -> bool:
        segment = path[-1]
        return (
            isinstance(segment, str)
            and _SENSITIVE_OPTION_PATTERN.search(segment) is not None
        )

    def _has_explicit_descendant(self, pointer: str) -> bool:
        prefix = f"{pointer}/"
        return any(candidate.startswith(prefix) for candidate in self.explicit_pointers)


def _json_path_exists(value: JsonValue, segments: tuple[str, ...]) -> bool:
    current = value
    for segment in segments:
        if isinstance(current, dict):
            if segment not in current:
                return False
            current = current[segment]
            continue
        if isinstance(current, list):
            if _JSON_ARRAY_INDEX.fullmatch(segment) is None:
                return False
            index = int(segment)
            if index >= len(current):
                return False
            current = current[index]
            continue
        return False
    return True


def _decode_json_pointer_token(token: str) -> str:
    if _INVALID_JSON_POINTER_ESCAPE.search(token) is not None:
        raise ValueError(
            "The sensitive option JSON Pointer segment "
            f"{token!r} is invalid: '~' must be followed by '0' or '1'. "
            "Use '~0' for '~' and '~1' for '/'."
        )
    return token.replace("~1", "/").replace("~0", "~")


def _encode_json_pointer_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")
