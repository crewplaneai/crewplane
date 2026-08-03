from __future__ import annotations

import errno
import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, payload: Any, ensure_parent: bool = True) -> Path:
    text = json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n"
    return atomic_write_text(path, text, ensure_parent)


def atomic_write_text(path: Path, content: str, ensure_parent: bool = True) -> Path:
    return atomic_write_bytes(path, content.encode("utf-8"), ensure_parent)


def atomic_write_bytes(path: Path, payload: bytes, ensure_parent: bool = True) -> Path:
    if ensure_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    publication_phase = "create temporary file"
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            publication_phase = "write temporary file"
            temp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            publication_phase = "sync temporary file"
            _fsync_file(handle.fileno())
        publication_phase = "replace target"
        temp_path.replace(path)
        publication_phase = "sync parent directory"
        _fsync_directory(path.parent)
        return path
    except Exception as exc:
        if temp_path is not None:
            with suppress(OSError):
                temp_path.unlink()
        if isinstance(exc, OSError):
            exc.add_note(
                f"Atomic publication failed for '{path}' during {publication_phase}."
            )
        raise


def atomic_write_json_if_absent(
    path: Path, payload: Any, ensure_parent: bool = True
) -> Path:
    text = json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n"
    return atomic_write_bytes_if_absent(path, text.encode("utf-8"), ensure_parent)


def atomic_write_bytes_if_absent(
    path: Path, payload: bytes, ensure_parent: bool = True
) -> Path:
    if ensure_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    publication_phase = "create temporary file"
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            publication_phase = "write temporary file"
            temp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            publication_phase = "sync temporary file"
            _fsync_file(handle.fileno())
        publication_phase = "publish target link"
        os.link(temp_path, path)
        publication_phase = "sync parent directory"
        _fsync_directory(path.parent)
        return path
    except OSError as exc:
        exc.add_note(
            f"Atomic publication failed for '{path}' during {publication_phase}."
        )
        raise
    finally:
        if temp_path is not None:
            with suppress(OSError):
                temp_path.unlink()


def _fsync_file(file_descriptor: int) -> None:
    os.fsync(file_descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if _directory_fsync_is_unsupported(exc):
            return
        raise
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if not _directory_fsync_is_unsupported(exc):
                raise
    finally:
        os.close(descriptor)


def _directory_fsync_is_unsupported(error: OSError) -> bool:
    unsupported_errnos = {
        errno.EINVAL,
        getattr(errno, "ENOTSUP", errno.EINVAL),
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    }
    return error.errno in unsupported_errnos
