from pathlib import Path

import pytest

from crewplane.core.file_text import (
    path_decoded_character_count,
    path_has_non_whitespace_text,
)


@pytest.mark.parametrize(
    ("payload", "count", "visible"),
    [
        (b"", 0, False),
        (b" \t\r\n", 4, False),
        ("\u2003\u00a0".encode(), 2, False),
        ("é🐍\r\n".encode(), 4, True),
        (b"\xff\xe2\x82", 2, True),
        (b" " * 65_535 + "🐍".encode(), 65_536, True),
        (b" " * 65_535 + "\u2003".encode(), 65_536, False),
        (b" " * 65_536 + b"\xe2", 65_537, True),
        (b" " * 131_072 + b"x", 131_073, True),
    ],
)
def test_utf8_file_scans_preserve_decoding_across_chunks(
    tmp_path: Path, payload: bytes, count: int, visible: bool
) -> None:
    path = tmp_path / "output.txt"
    path.write_bytes(payload)

    assert path_decoded_character_count(path) == count
    assert path_has_non_whitespace_text(path) is visible


def test_utf8_file_scans_do_not_materialize_whole_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "output.txt"
    path.write_bytes(b"x" * 131_072)

    def reject_whole_file_read(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("Whole-file reads are not allowed")

    monkeypatch.setattr(Path, "read_bytes", reject_whole_file_read)
    monkeypatch.setattr(Path, "read_text", reject_whole_file_read)
    assert path_decoded_character_count(path) == 131_072
    assert path_has_non_whitespace_text(path)
