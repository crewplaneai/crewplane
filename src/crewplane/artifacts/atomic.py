from __future__ import annotations

import errno
import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

_TEMPORARY_SUFFIX = ".tmp"


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
    if ensure_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    publication_phase = "create temporary file"
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=_temporary_name_prefix(path.name),
            suffix=_TEMPORARY_SUFFIX,
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
    return atomic_write_bytes_if_absent(path, json_bytes(payload), ensure_parent)


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
            prefix=_temporary_name_prefix(path.name),
            suffix=_TEMPORARY_SUFFIX,
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
