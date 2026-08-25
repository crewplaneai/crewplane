from __future__ import annotations

from typing import TypeGuard, TypeIs


def is_strict_int(value: object) -> TypeIs[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def is_nonnegative_int(value: object) -> TypeGuard[int]:
    return is_strict_int(value) and value >= 0


def positive_strict_int(value: object) -> int | None:
    if is_strict_int(value) and value > 0:
        return value
    return None
