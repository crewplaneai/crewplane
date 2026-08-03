from __future__ import annotations

import errno
import json
from unittest.mock import patch

import pytest

from crewplane.artifacts.atomic import (
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_json_if_absent,
    atomic_write_text,
)


def test_atomic_write_json_creates_parent_and_replaces_existing(tmp_path) -> None:
    path = tmp_path / "nested" / "payload.json"

    atomic_write_json(path, {"status": "running"})
    atomic_write_json(path, {"status": "succeeded"})

    assert json.loads(path.read_text(encoding="utf-8")) == {"status": "succeeded"}
    assert list(path.parent.glob("*.tmp")) == []


def test_atomic_write_text_and_bytes(tmp_path) -> None:
    text_path = tmp_path / "text.txt"
    bytes_path = tmp_path / "bytes.bin"

    atomic_write_text(text_path, "hello")
    atomic_write_bytes(bytes_path, b"\x00\x01")

    assert text_path.read_text(encoding="utf-8") == "hello"
    assert bytes_path.read_bytes() == b"\x00\x01"


def test_atomic_write_if_absent_does_not_replace_existing(tmp_path) -> None:
    path = tmp_path / "payload.json"

    atomic_write_json_if_absent(path, {"status": "running"})

    with pytest.raises(FileExistsError):
        atomic_write_json_if_absent(path, {"status": "succeeded"})

    assert json.loads(path.read_text(encoding="utf-8")) == {"status": "running"}


def test_atomic_write_without_parent_creation_fails_when_parent_is_missing(
    tmp_path,
) -> None:
    path = tmp_path / "missing" / "payload.json"

    with pytest.raises(FileNotFoundError):
        atomic_write_json_if_absent(path, {"status": "running"}, ensure_parent=False)

    assert not path.exists()


def test_atomic_write_propagates_file_fsync_integrity_failure(tmp_path) -> None:
    path = tmp_path / "payload.json"
    failure = OSError(errno.EIO, "injected data sync failure")

    with (
        patch("crewplane.artifacts.atomic.os.fsync", side_effect=failure),
        pytest.raises(OSError, match="injected data sync failure") as raised,
    ):
        atomic_write_json(path, {"status": "succeeded"})

    assert not path.exists()
    assert any("sync temporary file" in note for note in raised.value.__notes__)


def test_atomic_write_propagates_directory_fsync_integrity_failure(tmp_path) -> None:
    path = tmp_path / "payload.json"
    failure = OSError(errno.ENOSPC, "injected directory sync failure")

    with (
        patch(
            "crewplane.artifacts.atomic.os.fsync",
            side_effect=[None, failure],
        ),
        pytest.raises(OSError, match="injected directory sync failure") as raised,
    ):
        atomic_write_json(path, {"status": "succeeded"})

    assert path.exists()
    assert any("sync parent directory" in note for note in raised.value.__notes__)


def test_atomic_write_propagates_replacement_failure_without_losing_target(
    tmp_path,
) -> None:
    path = tmp_path / "payload.json"
    path.write_text('{"status": "old"}\n', encoding="utf-8")
    failure = OSError(errno.EROFS, "injected replacement failure")

    with (
        patch(
            "crewplane.artifacts.atomic.Path.replace",
            side_effect=failure,
        ),
        pytest.raises(OSError, match="injected replacement failure") as raised,
    ):
        atomic_write_json(path, {"status": "new"})

    assert json.loads(path.read_text(encoding="utf-8")) == {"status": "old"}
    assert any("replace target" in note for note in raised.value.__notes__)
    assert not tuple(tmp_path.glob(".payload.json.*.tmp"))


def test_atomic_write_allows_unsupported_directory_fsync(tmp_path) -> None:
    path = tmp_path / "payload.json"

    with patch(
        "crewplane.artifacts.atomic.os.fsync",
        side_effect=[None, OSError(errno.EINVAL, "unsupported directory sync")],
    ):
        atomic_write_json(path, {"status": "succeeded"})

    assert json.loads(path.read_text(encoding="utf-8")) == {"status": "succeeded"}


def test_atomic_json_rejects_nonfinite_numbers(tmp_path) -> None:
    with pytest.raises(ValueError, match="Out of range float values"):
        atomic_write_json(tmp_path / "payload.json", {"timeout": float("inf")})
