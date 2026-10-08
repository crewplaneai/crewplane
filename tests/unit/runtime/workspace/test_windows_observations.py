"""Exercise Windows enumeration policy with local I/O; native handles have separate tests."""

import os
from contextlib import contextmanager

import pytest

from crewplane.architecture import safe_files_windows
from crewplane.architecture.safe_file_reads import open_regular_file
from crewplane.architecture.windows_file_handles import FILE_LIST_DIRECTORY
from crewplane.runtime.workspace import snapshot_scan, snapshot_scan_io
from tests.helpers.windows_file_handles import LocalHandle


@pytest.fixture(autouse=True)
def local_handles(monkeypatch):
    @contextmanager
    def protection(path, list_entries=False):  # noqa: ARG001 - Test double signature.
        handle = LocalHandle(path)
        try:
            handle.validate(directory=True)
            yield handle
            handle.validate(directory=True)
        finally:
            handle.close()

    if os.name != "nt":
        monkeypatch.setattr(snapshot_scan_io, "protected_directory", protection)
    monkeypatch.setattr(snapshot_scan_io, "open_regular_file", open_regular_file)


def observe(root, policy=None):
    resolved = policy or snapshot_scan.WorkspaceSnapshotPolicy()
    budget = snapshot_scan.WorkspaceSnapshotBudget(resolved, resolved.clock())
    entries = {}
    snapshot_scan_io.scan_windows_directory(root, "", entries, budget)
    return dict(sorted(entries.items()))


def test_windows_enumeration_preserves_fingerprint_and_tracks_changes(tmp_path):
    (tmp_path / "nested").mkdir()
    path = tmp_path / "nested" / "café.txt"
    path.write_bytes(b"a\r\n\x1a")
    before = observe(tmp_path)
    assert before == snapshot_scan.snapshot_entries(tmp_path)
    path.write_bytes(b"changed\n")
    assert observe(tmp_path) != before


@pytest.mark.skipif(
    os.name != "posix",
    reason="Access-aware surrogate requires POSIX directory descriptors",
)
def test_windows_observation_enumerates_with_required_handle_access(
    tmp_path, monkeypatch
):
    tmp_path = tmp_path.resolve()
    calls = []
    entry_names = LocalHandle.entry_names

    def open_handle(path, access, share):
        calls.append(("open", path, access, share))
        return LocalHandle(path, calls)

    def list_entries(handle):
        access = next(
            call[2]
            for call in reversed(calls)
            if call[0] == "open" and call[1] == handle.path
        )
        if not access & FILE_LIST_DIRECTORY:
            raise PermissionError("Directory handle lacks FILE_LIST_DIRECTORY")
        return entry_names(handle)

    monkeypatch.setattr(safe_files_windows, "open_handle", open_handle)
    monkeypatch.setattr(LocalHandle, "entry_names", list_entries)
    monkeypatch.setattr(
        snapshot_scan_io, "protected_directory", safe_files_windows.protected_directory
    )
    nested = tmp_path / "nested"
    nested.mkdir()
    payload = b"exact\r\n\x1a"
    (nested / "café.txt").write_bytes(payload)

    entries = observe(tmp_path)
    metadata = (nested / "café.txt").stat()
    assert entries["nested/café.txt"] == snapshot_scan.snapshot_entry_digest(
        "nested/café.txt", metadata, "file", payload
    )
    assert set(entries) == {"nested", "nested/café.txt"}
    assert all(call[3] & 4 == 0 for call in calls if call[0] == "open")
    assert all(
        not call[2] & FILE_LIST_DIRECTORY
        for call in calls
        if call[0] == "open" and call[1] in tmp_path.parents
    )


def test_windows_exclusions_are_case_insensitive(tmp_path):
    (tmp_path / ".CREWPLANE").mkdir()
    (tmp_path / ".CREWPLANE" / "ignored").write_bytes(b"private")
    (tmp_path / "visible").write_bytes(b"yes")
    assert list(
        observe(
            tmp_path,
            snapshot_scan.WorkspaceSnapshotPolicy(
                excluded_roots=frozenset({".crewplane"})
            ),
        )
    ) == ["visible"]


@pytest.mark.parametrize(
    "policy",
    [
        snapshot_scan.WorkspaceSnapshotPolicy(max_entries=1),
        snapshot_scan.WorkspaceSnapshotPolicy(max_file_bytes=1),
        snapshot_scan.WorkspaceSnapshotPolicy(cancel_requested=lambda: True),
    ],
)
def test_windows_observation_limits_remain_conservative(tmp_path, policy):
    (tmp_path / "a").write_bytes(b"payload")
    (tmp_path / "b").write_bytes(b"payload")
    with pytest.raises(snapshot_scan.WorkspaceSnapshotError):
        observe(tmp_path, policy)


def test_windows_observation_sharing_failure_is_unreliable(tmp_path, monkeypatch):
    (tmp_path / "a").write_bytes(b"payload")

    def unavailable(path):
        raise PermissionError(str(path))

    monkeypatch.setattr(snapshot_scan_io, "open_regular_file", unavailable)
    with pytest.raises(snapshot_scan.WorkspaceSnapshotRaceError, match="safely read"):
        observe(tmp_path)
