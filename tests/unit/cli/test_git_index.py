from pathlib import Path

import pytest

from crewplane.cli.run.workspace.git_index import (
    git_index_extensions,
    index_checksum_length,
    read_index_extensions,
    skip_v4_path_prefix_length,
    validate_index_extensions,
)
from crewplane.cli.run.workspace.git_source import GitSourceContext
from crewplane.cli.run.workspace.source_types import WorkspacePolicyBuilder


def index_payload(
    version: int = 2,
    entries: tuple[bytes, ...] = (),
    extensions: tuple[tuple[bytes, bytes], ...] = (),
    checksum_length: int = 20,
) -> bytes:
    header = b"DIRC" + version.to_bytes(4, "big") + len(entries).to_bytes(4, "big")
    encoded_extensions = b"".join(
        signature + len(body).to_bytes(4, "big") + body
        for signature, body in extensions
    )
    return header + b"".join(entries) + encoded_extensions + bytes(checksum_length)


def v2_entry(path: bytes = b"a", extended: bool = False) -> bytes:
    flags = 0x4000 if extended else 0
    entry = bytes(60) + flags.to_bytes(2, "big")
    if extended:
        entry += bytes(2)
    entry += path + b"\0"
    return entry + bytes((-len(entry)) % 8)


def v4_entry(
    path: bytes = b"a",
    prefix_length: bytes = b"\0",
    checksum_length: int = 20,
) -> bytes:
    return bytes(40 + checksum_length + 2) + prefix_length + path + b"\0"


def git_context(git_dir: Path, object_format: str = "sha1") -> GitSourceContext:
    return GitSourceContext(
        run_base_commit="a" * 40,
        source_tree="b" * 40,
        object_format=object_format,
        git_top_level=git_dir.parent,
        project_root_relative_path=".",
        active_git_dir=git_dir,
        common_git_dir=git_dir,
        git_version="2.34.1",
    )


def test_parses_entries_and_extensions_for_supported_index_versions() -> None:
    assert git_index_extensions(
        index_payload(
            entries=(v2_entry(), v2_entry(b"extended", extended=True)),
            extensions=((b"TREE", b"tree-data"), (b"REUC", b"")),
        ),
        "sha1",
    ) == ("TREE", "REUC")
    assert git_index_extensions(
        index_payload(
            version=4,
            entries=(v4_entry(prefix_length=b"\x80\x00", checksum_length=32),),
            extensions=((b"EOIE", b"offset"),),
            checksum_length=32,
        ),
        "sha256",
    ) == ("EOIE",)


@pytest.mark.parametrize(
    ("payload", "object_format", "message"),
    [
        pytest.param(b"short", "sha1", "too short", id="short-file"),
        pytest.param(
            b"NOPE" + bytes(8 + 20),
            "sha1",
            "header is invalid",
            id="invalid-header",
        ),
        pytest.param(
            index_payload(version=1),
            "sha1",
            "version 1 is unsupported",
            id="unsupported-version",
        ),
        pytest.param(
            index_payload(),
            "unknown",
            "object format 'unknown' is unsupported",
            id="unsupported-object-format",
        ),
        pytest.param(
            index_payload(entries=(bytes(10),)),
            "sha1",
            "entry is truncated",
            id="truncated-entry",
        ),
        pytest.param(
            index_payload(entries=(bytes(60) + b"\x40\x00",)),
            "sha1",
            "extended index entry flags are truncated",
            id="truncated-extended-flags",
        ),
        pytest.param(
            index_payload(entries=(bytes(62) + b"unterminated",)),
            "sha1",
            "path is unterminated",
            id="unterminated-v2-path",
        ),
        pytest.param(
            index_payload(entries=(bytes(62) + b"ab\0",)),
            "sha1",
            "padding is truncated",
            id="truncated-v2-padding",
        ),
        pytest.param(
            index_payload(version=4, entries=(bytes(62) + b"\x80",)),
            "sha1",
            "prefix length is truncated",
            id="truncated-v4-prefix",
        ),
        pytest.param(
            index_payload(version=4, entries=(bytes(62) + b"\0unterminated",)),
            "sha1",
            "path is unterminated",
            id="unterminated-v4-path",
        ),
        pytest.param(
            index_payload(extensions=((b"ABC", b""),))[:-21] + bytes(20),
            "sha1",
            "extension header is truncated",
            id="truncated-extension-header",
        ),
        pytest.param(
            index_payload(extensions=((b"TEST", b"body"),))[:-22] + bytes(20),
            "sha1",
            "extension 'TEST' is truncated",
            id="truncated-extension-body",
        ),
    ],
)
def test_rejects_corrupt_or_unsupported_indexes(
    payload: bytes,
    object_format: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        git_index_extensions(payload, object_format)


def test_index_checksum_lengths_are_explicit() -> None:
    assert index_checksum_length("sha1") == 20
    assert index_checksum_length("sha256") == 32


def test_v4_prefix_reader_accepts_terminal_byte() -> None:
    assert skip_v4_path_prefix_length(b"\x80\x00path", 0, 6) == 2


def test_extension_reader_replaces_non_ascii_signatures() -> None:
    payload = b"\xffABC" + (0).to_bytes(4, "big")

    assert read_index_extensions(payload, 0, len(payload)) == ("�ABC",)


def test_validation_reports_missing_and_corrupt_index(tmp_path: Path) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    builder = WorkspacePolicyBuilder()

    validate_index_extensions(git_context(git_dir), builder)
    assert builder.errors == ["Workspace source policy failed: Git index is missing."]

    (git_dir / "index").write_bytes(b"corrupt")
    builder = WorkspacePolicyBuilder()
    validate_index_extensions(git_context(git_dir), builder)
    assert len(builder.errors) == 1
    assert "Git index is corrupt or unsupported" in builder.errors[0]


def test_validation_reports_unsupported_and_unknown_extensions(tmp_path: Path) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "index").write_bytes(
        index_payload(
            extensions=((b"FSMN", b"state"), (b"ZZZZ", b"unknown")),
        )
    )
    builder = WorkspacePolicyBuilder()

    validate_index_extensions(git_context(git_dir), builder)

    assert len(builder.errors) == 2
    assert "unsupported state: fsmonitor" in builder.errors[0]
    assert "unsupported extensions: ZZZZ" in builder.errors[1]


def test_validation_accepts_allowlisted_extensions(tmp_path: Path) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "index").write_bytes(
        index_payload(extensions=((b"TREE", b"tree-data"),))
    )
    builder = WorkspacePolicyBuilder()

    validate_index_extensions(git_context(git_dir), builder)

    assert builder.errors == []
