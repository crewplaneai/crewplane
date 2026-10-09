"""Exercise Windows enumeration policy with local I/O; native handles have separate tests."""

import os
import stat
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from crewplane.architecture import safe_files_windows
from crewplane.architecture.safe_file_reads import open_regular_file
from crewplane.architecture.windows_file_handles import (
    FILE_ATTRIBUTE_REPARSE_POINT,
    FILE_LIST_DIRECTORY,
)
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


@pytest.mark.parametrize(
    "failure",
    [
        None,
        PermissionError("sharing denied"),
        ValueError("unsafe file"),
        snapshot_scan.WorkspaceSnapshotLimitError("byte limit"),
        snapshot_scan.WorkspaceSnapshotCancelled("cancelled"),
    ],
    ids=["success", "sharing", "validation", "limit", "cancellation"],
)
def test_windows_scan_preserves_order_partial_results_and_protection(
    tmp_path, monkeypatch, failure
):
    (tmp_path / "z").write_bytes(b"z")
    nested = tmp_path / "a"
    nested.mkdir()
    (nested / "c").write_bytes(b"c")
    (nested / "b").write_bytes(b"b")
    protection = snapshot_scan_io.protected_directory
    file_open = snapshot_scan_io.open_regular_file
    entry_names = LocalHandle.entry_names
    active = []
    opened_directories = []
    descriptors = []
    reads = []

    def reverse_entry_names(handle):
        return iter(sorted(entry_names(handle), reverse=True))

    @contextmanager
    def tracked_protection(path, list_entries=False):
        with protection(path, list_entries=list_entries) as handle:
            scope = (path, list_entries)
            active.append(scope)
            opened_directories.append(scope)
            try:
                yield handle
            finally:
                assert active.pop() == scope

    @contextmanager
    def tracked_file_open(path):
        expected = [(tmp_path, True)]
        if path.parent == nested:
            expected.extend([(nested, False), (nested, True)])
        assert active == expected
        reads.append(path.relative_to(tmp_path).as_posix())
        if failure is not None and path == nested / "c":
            raise failure
        with file_open(path) as descriptor:
            descriptors.append(descriptor)
            yield descriptor

    monkeypatch.setattr(LocalHandle, "entry_names", reverse_entry_names)
    monkeypatch.setattr(snapshot_scan_io, "protected_directory", tracked_protection)
    monkeypatch.setattr(snapshot_scan_io, "open_regular_file", tracked_file_open)
    policy = snapshot_scan.WorkspaceSnapshotPolicy()
    budget = snapshot_scan.WorkspaceSnapshotBudget(policy, policy.clock())
    entries = {"existing": "kept"}

    if failure is None:
        assert (
            snapshot_scan_io.scan_windows_directory(tmp_path, "", entries, budget)
            is None
        )
        expected_paths = ["a", "a/b", "a/c", "z"]
        assert reads == ["a/b", "a/c", "z"]
        assert budget.file_bytes == 3
    else:
        with pytest.raises(snapshot_scan.WorkspaceSnapshotError) as caught:
            snapshot_scan_io.scan_windows_directory(tmp_path, "", entries, budget)
        if isinstance(failure, (OSError, ValueError)):
            assert type(caught.value) is snapshot_scan.WorkspaceSnapshotRaceError
            assert str(caught.value) == (
                f"Workspace observation could not safely read {nested}: {failure}"
            )
            assert caught.value.__cause__ is failure
        else:
            assert caught.value is failure
        expected_paths = ["a", "a/b"]
        assert reads == ["a/b", "a/c"]
        assert budget.file_bytes == 1

    assert list(entries) == ["existing", *expected_paths]
    assert entries["existing"] == "kept"
    for relative in expected_paths:
        path = tmp_path / relative
        directory = path.is_dir()
        assert entries[relative] == snapshot_scan.snapshot_entry_digest(
            relative,
            path.stat(),
            "dir" if directory else "file",
            b"" if directory else path.read_bytes(),
        )
    assert budget.entry_count == 4
    assert opened_directories == [(tmp_path, True), (nested, False), (nested, True)]
    assert active == []
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_windows_scan_rejects_directory_replacement_before_descent(
    tmp_path, monkeypatch
):
    root = tmp_path / "root"
    root.mkdir()
    nested = root / "nested"
    nested.mkdir()
    protection = snapshot_scan_io.protected_directory

    @contextmanager
    def replace_before_protection(path, list_entries=False):
        if path == nested:
            nested.rename(tmp_path / "original")
            nested.mkdir()
        with protection(path, list_entries=list_entries) as handle:
            yield handle

    monkeypatch.setattr(
        snapshot_scan_io, "protected_directory", replace_before_protection
    )
    policy = snapshot_scan.WorkspaceSnapshotPolicy()
    budget = snapshot_scan.WorkspaceSnapshotBudget(policy, policy.clock())
    entries = {}
    with pytest.raises(snapshot_scan.WorkspaceSnapshotRaceError) as caught:
        snapshot_scan_io.scan_windows_directory(root, "", entries, budget)
    assert str(caught.value) == "Workspace snapshot directory changed: nested"
    assert caught.value.__cause__ is None
    assert entries == {}
    assert budget.entry_count == 1
    assert budget.file_bytes == 0
    assert (tmp_path / "original").is_dir()
    assert nested.is_dir()


def test_windows_scan_translates_directory_exit_failure_after_recording_entries(
    tmp_path, monkeypatch
):
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "file").write_bytes(b"payload")
    protection = snapshot_scan_io.protected_directory
    failure = ValueError("directory changed on exit")

    @contextmanager
    def fail_after_protection(path, list_entries=False):
        with protection(path, list_entries=list_entries) as handle:
            yield handle
            if path == nested and not list_entries:
                raise failure

    monkeypatch.setattr(snapshot_scan_io, "protected_directory", fail_after_protection)
    policy = snapshot_scan.WorkspaceSnapshotPolicy()
    budget = snapshot_scan.WorkspaceSnapshotBudget(policy, policy.clock())
    entries = {}
    with pytest.raises(snapshot_scan.WorkspaceSnapshotRaceError) as caught:
        snapshot_scan_io.scan_windows_directory(tmp_path, "", entries, budget)
    assert str(caught.value) == (
        f"Workspace observation could not safely read {tmp_path}: {failure}"
    )
    assert caught.value.__cause__ is failure
    assert list(entries) == ["nested", "nested/file"]
    assert budget.entry_count == 2
    assert budget.file_bytes == 7


@pytest.mark.parametrize(
    ("mode", "attributes", "message"),
    [
        (
            stat.S_IFDIR,
            FILE_ATTRIBUTE_REPARSE_POINT,
            "Reparse redirection is unsupported in observations: unsafe",
        ),
        (
            stat.S_IFIFO,
            0,
            "Workspace snapshots support only directories, regular files, and "
            "symlinks; rejected 'unsafe' with mode 0o10000.",
        ),
    ],
    ids=["reparse-directory", "unsupported-type"],
)
def test_windows_scan_rejects_unsafe_discovered_metadata(
    tmp_path, monkeypatch, mode, attributes, message
):
    unsafe = tmp_path / "unsafe"
    unsafe.write_bytes(b"payload")
    original = unsafe.stat()
    identity = (original.st_dev, original.st_ino)
    metadata = SimpleNamespace(
        st_mode=mode,
        st_file_attributes=attributes,
        st_dev=original.st_dev,
        st_ino=original.st_ino,
        st_nlink=original.st_nlink,
    )
    fstat = os.fstat

    def read_metadata(descriptor):
        current = fstat(descriptor)
        return metadata if (current.st_dev, current.st_ino) == identity else current

    monkeypatch.setattr(snapshot_scan_io.os, "fstat", read_metadata)
    policy = snapshot_scan.WorkspaceSnapshotPolicy()
    budget = snapshot_scan.WorkspaceSnapshotBudget(policy, policy.clock())
    entries = {}
    with pytest.raises(snapshot_scan.WorkspaceSnapshotEntryError) as caught:
        snapshot_scan_io.scan_windows_directory(tmp_path, "", entries, budget)
    assert str(caught.value) == message
    assert caught.value.__cause__ is None
    assert entries == {}
