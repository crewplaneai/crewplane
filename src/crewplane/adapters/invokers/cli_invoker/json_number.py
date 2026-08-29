from __future__ import annotations

__all__ = [
    "JSON_NUMBER_TERMINAL_STATES",
    "JsonNumberError",
    "JsonNumberState",
    "advance_json_number",
    "start_json_number",
]

from typing import Literal


class JsonNumberError(ValueError):
    """Raised when a character violates the JSON number grammar."""


type JsonNumberState = Literal[
    "start",
    "sign",
    "zero",
    "integer",
    "fraction_start",
    "fraction",
    "exponent_start",
    "exponent_sign",
    "exponent",
]
type _JsonNumberToken = Literal[
    "zero",
    "digit",
    "decimal_point",
    "exponent_marker",
    "plus",
    "minus",
]

JSON_NUMBER_TERMINAL_STATES: frozenset[JsonNumberState] = frozenset(
    {"zero", "integer", "fraction", "exponent"}
)
_REPEATING_STATES: frozenset[JsonNumberState] = frozenset(
    {"integer", "fraction", "exponent"}
)
_TOKENS: dict[str, _JsonNumberToken] = {
    ".": "decimal_point",
    "E": "exponent_marker",
    "e": "exponent_marker",
    "+": "plus",
    "-": "minus",
}
_TRANSITIONS: dict[
    JsonNumberState,
    dict[_JsonNumberToken, JsonNumberState],
] = {
    "start": {"minus": "sign", "zero": "zero", "digit": "integer"},
    "sign": {"zero": "zero", "digit": "integer"},
    "zero": {
        "decimal_point": "fraction_start",
        "exponent_marker": "exponent_start",
    },
    "integer": {
        "decimal_point": "fraction_start",
        "exponent_marker": "exponent_start",
    },
    "fraction_start": {"zero": "fraction", "digit": "fraction"},
    "fraction": {"exponent_marker": "exponent_start"},
    "exponent_start": {
        "zero": "exponent",
        "digit": "exponent",
        "plus": "exponent_sign",
        "minus": "exponent_sign",
    },
    "exponent_sign": {"zero": "exponent", "digit": "exponent"},
    "exponent": {},
}


def start_json_number(first: str) -> JsonNumberState:
    """Start scanning a JSON number with its first character."""
    if first == "-":
        return "sign"
    if first == "0":
        return "zero"
    if first in "123456789":
        return "integer"
    raise JsonNumberError("Invalid JSON number.")


def advance_json_number(state: JsonNumberState, char: str) -> JsonNumberState:
    """Advance a JSON number state with one ASCII character."""
    if state in _REPEATING_STATES and char in "0123456789":
        return state
    token = _classify_char(char)
    if token is not None:
        next_state = _TRANSITIONS[state].get(token)
        if next_state is not None:
            return next_state
    raise JsonNumberError("Invalid JSON number.")


def _classify_char(char: str) -> _JsonNumberToken | None:
    if char == "0":
        return "zero"
    if char in "123456789":
        return "digit"
    return _TOKENS.get(char)
