from __future__ import annotations

from typing import TypeIs


def is_strict_int(value: object) -> TypeIs[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def positive_strict_int(value: object) -> int | None:
    if is_strict_int(value) and value > 0:
        return value
    return None
