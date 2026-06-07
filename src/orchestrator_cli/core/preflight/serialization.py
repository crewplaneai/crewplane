from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def to_json_safe(value: Any) -> Any:
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
    return value


def canonical_json(value: Any) -> str:
    """Return deterministic JSON for signing and persisted preflight artifacts."""

    return json.dumps(
        to_json_safe(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_json_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def pretty_sorted_json(value: Any) -> str:
    return json.dumps(
        to_json_safe(value),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
