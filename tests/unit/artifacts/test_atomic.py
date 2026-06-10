from __future__ import annotations

import json

import pytest

from orchestrator_cli.artifacts.atomic import (
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
