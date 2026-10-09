import hashlib
import sys

import pytest

from crewplane.core import file_hashing, file_hashing_io, state_paths
from crewplane.core.preflight.static_resources import resolve_static_file


def test_reserved_state_paths_follow_host_case_and_separator_rules(
    monkeypatch, tmp_path
):
    monkeypatch.setattr("crewplane.core.platform.platform.system", lambda: "Windows")
    assert state_paths.is_reserved_state_path(
        r".CREWPLANE\EXECUTION-STAGES\run", state_paths.RUNTIME_ARTIFACT_ROOTS
    )
    assert not state_paths.is_reserved_state_path(
        ".crewplane-work", state_paths.RUNTIME_ARTIFACT_ROOTS
    )
    assert (
        state_paths.project_root_from_config_path(tmp_path / ".CREWPLANE/config.yml")
        == tmp_path
    )


@pytest.mark.skipif(
    sys.platform != "win32", reason="Requires native Windows authored paths"
)
def test_native_authored_references_keep_allowlist_and_reject_drive_relative_ads(
    tmp_path,
):
    inside = tmp_path / "project"
    inside.mkdir()
    source = inside / "café.txt"
    source.write_bytes(b"content\r\n")
    assert (
        resolve_static_file(str(source), inside, inside, ()).payload == b"content\r\n"
    )
    assert resolve_static_file("café.txt", inside, inside, ()).resource is not None
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    assert resolve_static_file(str(outside), inside, inside, ()).diagnostics
    assert (
        resolve_static_file(str(outside), inside, inside, (outside,)).resource
        is not None
    )
    for value in ("C:relative", r"\rooted", "café.txt:stream"):
        assert resolve_static_file(value, inside, inside, ()).diagnostics
    reserved = inside / ".CREWPLANE/EXECUTION-STAGES/private"
    reserved.parent.mkdir(parents=True)
    reserved.write_bytes(b"private")
    assert resolve_static_file(str(reserved), inside, inside, ()).diagnostics


def test_windows_hash_uses_bounded_opened_bytes(tmp_path, monkeypatch):
    path = tmp_path / "source"
    payload = b"LF\nCRLF\r\n\x1a\xff" * 200000
    path.write_bytes(payload)
    monkeypatch.setattr(
        file_hashing_io,
        "file_hashing_operations",
        file_hashing_io.windows_file_hashing_operations,
    )
    expected = (len(payload), hashlib.sha256(payload).hexdigest())
    assert file_hashing.file_size_and_sha256(path) == expected
    assert file_hashing.sha256_file(path) == expected[1]
    original = file_hashing_io.bounded_file_chunks

    def change_after_read(descriptor):
        yield from original(descriptor)
        path.write_bytes(b"replaced")

    monkeypatch.setattr(file_hashing_io, "bounded_file_chunks", change_after_read)
    with pytest.raises((OSError, ValueError)):
        file_hashing.sha256_file(path)
