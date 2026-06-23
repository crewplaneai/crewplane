from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class NullableIntField:
    value: int | None
    valid: bool


def int_field(payload: Mapping[str, object], key: str) -> int | None:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def nullable_int_field(
    payload: Mapping[str, object],
    key: str,
) -> NullableIntField:
    value = payload.get(key)
    if value is None:
        return NullableIntField(value=None, valid=True)
    if isinstance(value, bool) or not isinstance(value, int):
        return NullableIntField(value=None, valid=False)
    return NullableIntField(value=value, valid=True)


def mapping_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def bool_field_matches(
    payload: dict[str, object],
    field_name: str,
    expected: bool,
) -> bool:
    value = payload.get(field_name)
    return isinstance(value, bool) and value == expected


def is_hex_object(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) in {40, 64}
        and all(char in "0123456789abcdef" for char in value)
    )
