import ctypes
import hashlib
import ntpath
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

import pytest

from crewplane.architecture.safe_file_reads import (
    copy_regular_file,
    read_contained_bytes,
)
from crewplane.architecture.safe_files import (
    contained_regular_file,
    ensure_contained_directory,
    replace_contained_file,
)
from crewplane.architecture.safe_files_windows import (
    protected_directory,
    protected_file,
)
from crewplane.artifacts.atomic import atomic_write_bytes
from crewplane.runtime.execution.provider_call.provider_output import (
    bind_invocation_output,
    read_bound_invocation_output,
)
from tests.helpers.platforms import extended_file_test_root

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Requires native Windows handle and NTFS semantics"
)


def test_native_creates_directories_without_recreating_drive_root(tmp_path) -> None:
    directory = ensure_contained_directory(tmp_path, "new/nested")
    assert directory.is_dir()
    assert ensure_contained_directory(tmp_path, "new/nested") == directory


@pytest.mark.parametrize("extended", [False, True])
def test_native_short_path_aliases_preserve_protected_reads(tmp_path, extended):
    directory = tmp_path / "directory with a long name"
    directory.mkdir()
    source = directory / "source.txt"
    source.write_bytes(b"exact\r\n\xff\x1a")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    api.GetShortPathNameW.restype = wintypes.DWORD
    needed = api.GetShortPathNameW(str(directory), None, 0)
    assert needed
    buffer = ctypes.create_unicode_buffer(needed)
    assert 0 < api.GetShortPathNameW(str(directory), buffer, len(buffer)) < needed
    if ntpath.normcase(buffer.value) == ntpath.normcase(str(directory)):
        pytest.skip("The filesystem does not provide an 8.3 alias")
    alias = Path("\\\\?\\" + buffer.value if extended else buffer.value)

    with protected_directory(alias) as short, protected_directory(directory) as long:
        assert short.information().identity == long.information().identity
    assert read_contained_bytes(alias, source.name) == source.read_bytes()
    signature = bind_invocation_output(alias / source.name)
    assert read_bound_invocation_output(alias / source.name, signature) == (
        source.read_bytes().decode("utf-8", errors="replace")
    )


def test_native_rewritten_sources_keep_consistent_descriptor_and_path_signatures(
    tmp_path,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"initial")
    for payload in (b"rewritten\r\n\xff\x1a", b"changed again"):
        source.write_bytes(payload)
        signature = bind_invocation_output(source)
        assert read_bound_invocation_output(source, signature) == payload.decode(
            "utf-8", errors="replace"
        )
        assert read_contained_bytes(tmp_path, "source") == payload
        copy_regular_file(source, tmp_path / "copy")
        assert (tmp_path / "copy").read_bytes() == payload


def test_handles_block_directory_and_source_replacement(tmp_path) -> None:
    directory = tmp_path / "protected"
    directory.mkdir()
    source = directory / "bytes"
    source.write_bytes(b"stable")
    with protected_file(source):
        for operation in (
            lambda: source.unlink(),
            lambda: source.write_bytes(b"changed"),
            lambda: directory.rename(tmp_path / "moved"),
        ):
            with pytest.raises(OSError):
                operation()
    source.unlink()


def test_directory_handle_blocks_rename_without_open_child(tmp_path: Path) -> None:
    directory = tmp_path / "protected"
    directory.mkdir()
    moved = tmp_path / "moved"

    with protected_directory(directory):
        with pytest.raises(OSError) as error:
            directory.rename(moved)
        assert error.value.winerror == 32
        assert directory.is_dir()
        assert not moved.exists()

    directory.rename(moved)
    assert moved.is_dir()


def test_native_junctions_and_hardlinks_are_rejected(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"secret")
    junction = tmp_path / "junction"
    subprocess.run(
        [
            os.environ["COMSPEC"],
            "/d",
            "/c",
            "mklink",
            "/J",
            str(junction),
            str(outside),
        ],
        check=True,
        capture_output=True,
    )
    assert contained_regular_file(tmp_path, "junction/secret") is None
    with pytest.raises(ValueError):
        read_contained_bytes(tmp_path, "junction/secret")
    os.link(outside / "secret", tmp_path / "hardlink")
    assert contained_regular_file(tmp_path, "hardlink") is None


def test_native_publication_does_not_overwrite_and_finishes_single_link(
    tmp_path,
) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"exact\r\n\x1a\xff")
    target.write_bytes(b"competitor")
    with pytest.raises(FileExistsError):
        replace_contained_file(tmp_path, "target", source)
    assert target.read_bytes() == b"competitor"
    target.unlink()
    replace_contained_file(tmp_path, "target", source)
    assert target.stat().st_nlink == 1
    assert not source.exists()
    assert read_contained_bytes(tmp_path, "target") == b"exact\r\n\x1a\xff"


def test_native_publication_rollback_keeps_competing_replacement(
    tmp_path, monkeypatch
) -> None:
    from crewplane.architecture.windows_file_handles import FileHandle

    source = tmp_path / "source"
    source.write_bytes(b"ours")
    target = tmp_path / "target"
    original = FileHandle.open_child

    def replace_before_delete(
        parent, name, access=0x80, share=1, disposition=1, directory=None
    ):
        path = parent.path / name
        if path == source and access & 0x10000:
            target.unlink()
            target.write_bytes(b"competitor")
            raise OSError("publication interrupted")
        return original(parent, name, access, share, disposition, directory)

    monkeypatch.setattr(FileHandle, "open_child", replace_before_delete)
    with pytest.raises(OSError, match="interrupted"):
        replace_contained_file(tmp_path, "target", source)
    assert target.read_bytes() == b"competitor"
    assert source.read_bytes() == b"ours"


def test_native_sharing_failure_keeps_old_metadata_and_removes_temporary(
    tmp_path,
) -> None:
    target = tmp_path / "metadata.json"
    target.write_bytes(b"old")
    with protected_file(target), pytest.raises(OSError):
        atomic_write_bytes(target, b"new")
    assert target.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [target]


def test_native_copy_bytes_and_long_paths(tmp_path) -> None:
    tmp_path = extended_file_test_root(tmp_path)
    relative = "/".join(["long-component-" + "x" * 45] * 5)
    try:
        directory = ensure_contained_directory(tmp_path, relative)
    except OSError as error:
        assert error.winerror in {3, 206}
        assert "long-path" in " ".join(error.__notes__)
        return
    payload = "café 🌍\r\n\x1a".encode() * 200000
    source = directory / "source"
    source.write_bytes(payload)
    copy_regular_file(source, directory / "copy")
    actual = read_contained_bytes(directory, "copy")
    assert hashlib.sha256(actual).digest() == hashlib.sha256(payload).digest()
    with protected_directory(directory):
        assert (directory / "copy").stat().st_nlink == 1


def test_native_generated_snapshot_copies_root_files_and_metadata(tmp_path):
    from crewplane.artifacts.generated_files.catalog import (
        snapshot_generated_file_workspace,
    )

    source = tmp_path / "workspace"
    source.mkdir()
    payload = b"captured\r\n\x1a\xff" * 200000
    generated = source / "report.bin"
    generated.write_bytes(payload)
    output = tmp_path / "provider.md"
    output.write_bytes(b"## Generated Files\n[report](report.bin)\n")
    snapshot = snapshot_generated_file_workspace(
        output, source, candidate_files=(generated,)
    )
    assert (snapshot / "report.bin").read_bytes() == payload
    assert (
        (snapshot / ".crewplane-generated-file-source.json")
        .read_bytes()
        .endswith(b"\n")
    )


@pytest.mark.parametrize("depth", [0, 7], ids=["short", "over-260-characters"])
def test_native_extended_path_reads_preserved_bytes(tmp_path, depth):
    root = tmp_path.resolve()
    extended = root if str(root).startswith("\\\\?\\") else Path("\\\\?\\" + str(root))
    directory = ensure_contained_directory(
        extended, "/".join(["component" * 4] * depth)
    )
    source = directory / "source"
    source.write_bytes(b"LF\nCRLF\r\nCtrl-Z\x1a\xff")
    if depth:
        assert len(str(source)) > 260
    assert (
        read_contained_bytes(extended, source.relative_to(extended).as_posix())
        == source.read_bytes()
    )


@pytest.mark.parametrize("failure", ["copy", "validation", "publication"])
def test_native_publication_cleanup_blocks_parent_replacement(
    tmp_path, monkeypatch, failure
):
    from crewplane.architecture import windows_file_handles
    from crewplane.runtime.execution.provider_call import provider_output
    from crewplane.runtime.execution.publication_registry import (
        RuntimePublicationRegistry,
    )

    source = tmp_path / "source"
    source.write_bytes(b"trusted")
    signature = provider_output.bind_invocation_output(source)
    if failure == "validation":
        signature = signature[0], "0" * 64
    destination = tmp_path / "destination"
    destination.mkdir()
    attempted = False
    original_delete = windows_file_handles.FileHandle.delete
    original_open_child = windows_file_handles.FileHandle.open_child
    original_unlink = Path.unlink

    def check_parent(path):
        nonlocal attempted
        if path.parent == destination and path.name.startswith(
            ".crewplane-publication-"
        ):
            attempted = True
            with pytest.raises(OSError) as error:
                destination.rename(tmp_path / "replaced")
            assert error.value.winerror == 32

    def delete(handle):
        check_parent(handle.path)
        original_delete(handle)

    def open_child(parent, name, access=0x80, share=1, disposition=1, directory=None):
        if disposition == 2:
            check_parent(parent.path / name)
        return original_open_child(parent, name, access, share, disposition, directory)

    def unlink(path, missing_ok=False):
        check_parent(path)
        original_unlink(path, missing_ok=missing_ok)

    def fail(*args):
        assert args
        raise OSError("publication interrupted")

    monkeypatch.setattr(windows_file_handles.FileHandle, "delete", delete)
    monkeypatch.setattr(windows_file_handles.FileHandle, "open_child", open_child)
    monkeypatch.setattr(Path, "unlink", unlink)
    if failure == "copy":
        monkeypatch.setattr(provider_output.os, "fsync", fail)
    elif failure == "publication":
        monkeypatch.setattr(provider_output, "replace_contained_file", fail)
    registry = RuntimePublicationRegistry()
    try:
        with pytest.raises((OSError, RuntimeError)):
            provider_output.publish_invocation_output(
                source, destination / "published", registry, signature
            )
        assert attempted
        assert list(destination.iterdir()) == []
        assert source.read_bytes() == b"trusted"
        assert registry.snapshot() == ({}, 0)
    finally:
        registry.close()


def test_native_publication_cleanup_preserves_competing_stage(tmp_path, monkeypatch):
    from crewplane.runtime.execution.provider_call import provider_output
    from crewplane.runtime.execution.publication_registry import (
        RuntimePublicationRegistry,
    )

    source = tmp_path / "source"
    source.write_bytes(b"trusted")
    competitor = None

    def replace_and_fail(root, relative, staged):
        nonlocal competitor
        assert root == tmp_path and relative == "published"
        staged.rename(tmp_path / "retired-stage")
        staged.write_bytes(b"competitor")
        competitor = staged
        raise OSError("publication interrupted")

    monkeypatch.setattr(provider_output, "replace_contained_file", replace_and_fail)
    registry = RuntimePublicationRegistry()
    try:
        with pytest.raises(RuntimeError, match="unsafe destination"):
            provider_output.publish_invocation_output(
                source, tmp_path / "published", registry
            )
        assert competitor is not None
        assert competitor.read_bytes() == b"competitor"
        assert (tmp_path / "retired-stage").read_bytes() == b"trusted"
        assert registry.snapshot() == ({}, 0)
    finally:
        registry.close()


@pytest.mark.parametrize("existing", [False, True], ids=["creation", "replacement"])
def test_native_snapshot_mutations_block_parent_replacement(
    tmp_path, monkeypatch, existing
):
    from crewplane.architecture import windows_file_handles
    from crewplane.artifacts.generated_files.catalog import (
        snapshot_generated_file_workspace,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = tmp_path / "provider.md"
    output.write_bytes(b"No generated files.\n")
    snapshot = tmp_path / "snapshots" / "candidate"
    snapshot.parent.mkdir()
    if existing:
        nested = snapshot / "nested"
        nested.mkdir(parents=True)
        (nested / "stale").write_bytes(b"stale")
    mutations = []
    original_open_child = windows_file_handles.FileHandle.open_child
    original_delete = windows_file_handles.FileHandle.delete

    def check_parent(path):
        if path == snapshot or snapshot in path.parents:
            mutations.append(path)
            with pytest.raises(OSError) as error:
                snapshot.parent.rename(tmp_path / "replaced")
            assert error.value.winerror == 32

    def open_child(parent, name, access=0x80, share=1, disposition=1, directory=None):
        if disposition in {2, 3}:
            check_parent(parent.path / name)
        return original_open_child(parent, name, access, share, disposition, directory)

    def delete(handle):
        check_parent(handle.path)
        if handle.path == snapshot or snapshot in handle.path.parents:
            with pytest.raises(OSError) as error:
                handle.path.rename(tmp_path / "replaced-entry")
            assert error.value.winerror == 32
        original_delete(handle)

    monkeypatch.setattr(windows_file_handles.FileHandle, "open_child", open_child)
    monkeypatch.setattr(windows_file_handles.FileHandle, "delete", delete)
    assert (
        snapshot_generated_file_workspace(output, workspace, snapshot_root=snapshot)
        == snapshot
    )
    assert mutations
    assert not (snapshot / "nested").exists()
    assert (snapshot / ".crewplane-generated-file-source.json").is_file()


def test_native_snapshot_reset_rejects_nested_junction(tmp_path):
    from crewplane.artifacts.generated_files.catalog import (
        snapshot_generated_file_workspace,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = tmp_path / "provider.md"
    output.write_bytes(b"No generated files.\n")
    snapshot = tmp_path / "snapshots" / "candidate"
    snapshot.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    external_file = outside / "keep"
    external_file.write_bytes(b"external bytes")
    subprocess.run(
        [
            os.environ["COMSPEC"],
            "/d",
            "/c",
            "mklink",
            "/J",
            str(snapshot / "redirect"),
            str(outside),
        ],
        check=True,
        capture_output=True,
    )
    with pytest.raises(RuntimeError, match="not a directory"):
        snapshot_generated_file_workspace(output, workspace, snapshot_root=snapshot)
    assert external_file.read_bytes() == b"external bytes"
    assert list(outside.iterdir()) == [external_file]


@pytest.mark.parametrize(
    "operation", ["snapshot", "directories", "metadata", "publication", "delete"]
)
def test_native_in_place_junction_mutation_never_touches_external_files(
    tmp_path, monkeypatch, operation
):
    from crewplane.architecture.windows_file_handles import FileHandle
    from crewplane.artifacts.generated_files.catalog import (
        snapshot_generated_file_workspace,
    )
    from crewplane.runtime.execution.provider_call import provider_output
    from crewplane.runtime.execution.publication_registry import (
        RuntimePublicationRegistry,
    )
    from tests.helpers.windows_junctions import set_junction

    outside = tmp_path / "outside"
    outside.mkdir()
    external_file = outside / "keep"
    external_file.write_bytes(b"external bytes")
    probe = tmp_path / "probe"
    probe.mkdir()
    set_junction(probe, outside)
    assert probe.is_junction()
    probe.rmdir()

    guarded = tmp_path / "guarded"
    guarded.mkdir()
    source = tmp_path / "provider.md"
    source.write_bytes(b"No generated files.\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    snapshot = guarded if operation == "delete" else guarded / "candidate"
    original = FileHandle.validate
    mutated = False

    def mutate(handle, directory, links=1):
        nonlocal mutated
        result = original(handle, directory, links)
        if handle.path == guarded and not mutated:
            mutated = True
            set_junction(guarded, outside)
        return result

    monkeypatch.setattr(FileHandle, "validate", mutate)
    registry = RuntimePublicationRegistry()
    try:
        try:
            if operation in {"snapshot", "delete"}:
                snapshot_generated_file_workspace(
                    source, workspace, snapshot_root=snapshot
                )
            elif operation == "directories":
                ensure_contained_directory(guarded, "new/nested")
            elif operation == "metadata":
                atomic_write_bytes(guarded / "metadata.json", b"ours")
            else:
                provider_output.publish_invocation_output(
                    source, guarded / "published", registry
                )
        except (OSError, ValueError, RuntimeError):
            pass
        assert mutated
        assert external_file.read_bytes() == b"external bytes"
        assert list(outside.iterdir()) == [external_file]
    finally:
        registry.close()
