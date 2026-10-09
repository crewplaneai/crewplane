import os
from unittest.mock import patch

import pytest

from crewplane.architecture.safe_file_reads import (
    copy_regular_file,
    read_contained_bytes,
)


@pytest.mark.parametrize(
    "payload",
    [b"LF\n", b"CRLF\r\nCtrl-Z\x1a\xff", "Unicode 日本語 🌍".encode() * 130000],
    ids=["lf", "crlf-control-invalid", "unicode-large"],
)
def test_bounded_read_and_copy_preserve_bytes(tmp_path, payload) -> None:
    source = tmp_path / "source"
    source.write_bytes(payload)
    copy_regular_file(source, tmp_path / "copy")
    assert read_contained_bytes(tmp_path, "source", len(payload)) == payload
    assert (tmp_path / "copy").read_bytes() == payload
    with pytest.raises(ValueError, match="limit"):
        read_contained_bytes(tmp_path, "source", len(payload) - 1)


def test_copy_refuses_hardlinked_destination_without_truncating(tmp_path) -> None:
    source, target, alias = (tmp_path / name for name in ("source", "target", "alias"))
    source.write_bytes(b"new")
    target.write_bytes(b"old")
    os.link(target, alias)
    with pytest.raises(ValueError):
        copy_regular_file(source, target)
    assert target.read_bytes() == alias.read_bytes() == b"old"


def test_copy_handles_short_writes(tmp_path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"complete byte stream")
    original = os.write

    def short_write(descriptor, payload):
        return original(descriptor, payload[:3])

    with patch("crewplane.architecture.safe_file_reads.os.write", new=short_write):
        copy_regular_file(source, tmp_path / "copy")
    assert (tmp_path / "copy").read_bytes() == source.read_bytes()
