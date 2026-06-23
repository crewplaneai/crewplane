from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from crewplane.architecture.contracts import JsonValue


def to_json_safe(value: object) -> JsonValue:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, tuple):
        return [to_json_safe(item) for item in value]
    if isinstance(value, list):
        return [to_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_json_safe(item) for key, item in value.items()}
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"Value of type {type(value).__name__} is not JSON serializable.")


def canonical_json(value: object) -> str:
    """Return deterministic JSON for signing and persisted preflight artifacts."""

    return json.dumps(
        to_json_safe(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_json_bytes(value: object) -> bytes:
    return canonical_json(value).encode("utf-8")


def pretty_sorted_json(value: object) -> str:
    return json.dumps(
        to_json_safe(value),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
