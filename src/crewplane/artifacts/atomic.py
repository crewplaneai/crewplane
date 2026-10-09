from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

_TEMPORARY_SUFFIX = ".tmp"
type AtomicWriter = Callable[[Path, bytes, bool, tuple[str, str], bool], Path]


def atomic_writer() -> AtomicWriter:
    if os.name == "nt":
        from .atomic_windows import publish_bytes
    else:
        from .atomic_posix import publish_bytes
    return publish_bytes


def _temporary_name_prefix(target_name: str) -> str:
    return f".{target_name}."


def atomic_temporary_target_name(name: str) -> str | None:
    """Decode a temporary basename without imposing target-file eligibility."""
    if not name.startswith(".") or not name.endswith(_TEMPORARY_SUFFIX):
        return None
    target_and_token = name[1 : -len(_TEMPORARY_SUFFIX)]
    target_name, separator, token = target_and_token.rpartition(".")
    if not separator or not token or not target_name:
        return None
    return target_name


def json_bytes(payload: Any) -> bytes:
    return (
        json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def atomic_write_json(path: Path, payload: Any, ensure_parent: bool = True) -> Path:
    return atomic_write_bytes(path, json_bytes(payload), ensure_parent)


def atomic_write_text(path: Path, content: str, ensure_parent: bool = True) -> Path:
    return atomic_write_bytes(path, content.encode("utf-8"), ensure_parent)


def atomic_write_bytes(path: Path, payload: bytes, ensure_parent: bool = True) -> Path:
    return atomic_writer()(
        path,
        payload,
        ensure_parent,
        (_temporary_name_prefix(path.name), _TEMPORARY_SUFFIX),
        True,
    )


def atomic_write_json_if_absent(
    path: Path, payload: Any, ensure_parent: bool = True
) -> Path:
    return atomic_write_bytes_if_absent(path, json_bytes(payload), ensure_parent)


def atomic_write_bytes_if_absent(
    path: Path, payload: bytes, ensure_parent: bool = True
) -> Path:
    return atomic_writer()(
        path,
        payload,
        ensure_parent,
        (_temporary_name_prefix(path.name), _TEMPORARY_SUFFIX),
        False,
    )
