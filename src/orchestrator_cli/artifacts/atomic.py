from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, payload: Any, ensure_parent: bool = True) -> Path:
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    return atomic_write_text(path, text, ensure_parent)


def atomic_write_text(path: Path, content: str, ensure_parent: bool = True) -> Path:
    return atomic_write_bytes(path, content.encode("utf-8"), ensure_parent)


def atomic_write_bytes(path: Path, payload: bytes, ensure_parent: bool = True) -> Path:
    if ensure_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            _fsync_file(handle.fileno())
        temp_path.replace(path)
        _fsync_directory(path.parent)
        return path
    except Exception:
        if temp_path is not None:
            with suppress(OSError):
                temp_path.unlink()
        raise


def atomic_write_json_if_absent(
    path: Path, payload: Any, ensure_parent: bool = True
) -> Path:
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    return atomic_write_bytes_if_absent(path, text.encode("utf-8"), ensure_parent)


def atomic_write_bytes_if_absent(
    path: Path, payload: bytes, ensure_parent: bool = True
) -> Path:
    if ensure_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            _fsync_file(handle.fileno())
        os.link(temp_path, path)
        _fsync_directory(path.parent)
        return path
    finally:
        if temp_path is not None:
            with suppress(OSError):
                temp_path.unlink()


def _fsync_file(file_descriptor: int) -> None:
    with suppress(OSError):
        os.fsync(file_descriptor)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        _fsync_file(descriptor)
    finally:
        os.close(descriptor)
