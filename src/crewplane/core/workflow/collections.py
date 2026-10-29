"""Strict, bounded JSON data shared by foreach collections and result indexes."""

from __future__ import annotations

import json
import math
import re
from typing import cast

from crewplane.architecture.contracts import JsonObject

COLLECTION_BYTE_LIMIT = 1024 * 1024
COLLECTION_DEPTH_LIMIT = 64
COLLECTION_REFERENCE = re.compile(r"\{\{([a-z0-9._-]+)\.(output|findings)\}\}")


def collection_reference(value: str) -> tuple[str, str]:
    match = COLLECTION_REFERENCE.fullmatch(value.strip())
    if match is None:
        raise ValueError(
            "items must be an array of objects or exactly one {{node.output}} or {{node.findings}} reference"
        )
    return match[1], match[2]


def validate_collection(
    value: object, max_items: int, group: str, source: str = "items"
) -> list[JsonObject]:
    label = f"Foreach '{group}' {source}"
    if type(max_items) is not int or max_items <= 0:
        raise ValueError(f"{label}: max_items must be a strict positive integer")
    if not isinstance(value, list):
        raise ValueError(f"{label}: expected an array of objects")
    if len(value) > max_items:
        raise ValueError(f"{label}: {len(value)} items exceeds max_items {max_items}")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"{label}[{index}]: expected an object")
    _validate_json_value(value, 1, set(), label)
    _encode(value, label)
    return cast(list[JsonObject], value)


def _validate_json_value(
    value: object, depth: int, active: set[int], label: str
) -> None:
    if isinstance(value, dict | list):
        if depth > COLLECTION_DEPTH_LIMIT:
            raise ValueError(
                f"{label}: container depth exceeds {COLLECTION_DEPTH_LIMIT}"
            )
        identity = id(value)
        if identity in active:
            raise ValueError(f"{label}: collection contains a cycle")
        active.add(identity)
        try:
            if isinstance(value, dict):
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise ValueError(f"{label}: object keys must be strings")
                    _validate_json_value(key, depth + 1, active, label)
                    _validate_json_value(item, depth + 1, active, f"{label}.{key}")
            else:
                for index, item in enumerate(value):
                    _validate_json_value(item, depth + 1, active, f"{label}[{index}]")
        finally:
            active.remove(identity)
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{label}: numbers must be finite")
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeError as exc:
            raise ValueError(f"{label}: invalid UTF-8 string") from exc
    elif value is not None and type(value) not in (int, float, bool):
        raise ValueError(f"{label}: unsupported JSON value {type(value).__name__}")


def _encode(value: object, label: str) -> bytes:
    try:
        data = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ValueError(f"{label}: cannot encode collection: {exc}") from exc
    if len(data) > COLLECTION_BYTE_LIMIT:
        raise ValueError(
            f"{label}: collection byte limit {COLLECTION_BYTE_LIMIT} exceeded"
        )
    return data


def collection_bytes(
    value: object, max_items: int, group: str, source: str = "items"
) -> bytes:
    return _encode(
        validate_collection(value, max_items, group, source),
        f"Foreach '{group}' {source}",
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number {value}")


def decode_collection(
    data: bytes, max_items: int, group: str, source: str = "items"
) -> list[JsonObject]:
    label = f"Foreach '{group}' {source}"
    if len(data) > COLLECTION_BYTE_LIMIT:
        raise ValueError(
            f"{label}: collection byte limit {COLLECTION_BYTE_LIMIT} exceeded"
        )
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError(f"{label}: invalid JSON collection: {exc}") from exc
    return validate_collection(value, max_items, group, source)
