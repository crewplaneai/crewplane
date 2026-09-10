from __future__ import annotations

import os
from pathlib import Path

import pytest

from crewplane.architecture.ports.artifacts import StageTaskSpec, TerminalHistoryRead
from crewplane.architecture.safe_files import (
    contained_directory,
    contained_regular_file,
    ensure_contained_directory,
    replace_contained_file,
)


@pytest.mark.parametrize("create", [False, True])
@pytest.mark.parametrize("location", ["root", "component"])
@pytest.mark.parametrize("error_type", [PermissionError, OSError])
def test_directory_inspection_failures_never_create_descendants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    create: bool,
    location: str,
    error_type: type[OSError],
) -> None:
    root = tmp_path / "root"
    (root / "nested").mkdir(parents=True)
    blocked = root if location == "root" else root / "nested"
    original_lstat = Path.lstat
    failure = error_type("injected inspection failure")

    def inspect(path: Path) -> os.stat_result:
        if path == blocked:
            raise failure
        return original_lstat(path)

    operation = ensure_contained_directory if create else contained_directory
    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", inspect)
        with pytest.raises(
            PermissionError if error_type is PermissionError else ValueError
        ):
            operation(root, "nested/child")

    assert not (root / "nested" / "child").exists()


@pytest.mark.parametrize("create", [False, True])
@pytest.mark.parametrize("location", ["root", "component"])
@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_directory_paths_reject_files_and_symlinks(
    tmp_path: Path, create: bool, location: str, kind: str
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "external"
    target.mkdir()
    blocked = root if location == "root" else root / "nested"
    if blocked == root:
        root.rmdir()
    if kind == "file":
        blocked.write_text("keep", encoding="utf-8")
    else:
        blocked.symlink_to(target, target_is_directory=True)

    operation = ensure_contained_directory if create else contained_directory
    with pytest.raises(ValueError, match="real directory"):
        operation(root, "nested/child")

    assert list(target.iterdir()) == []


def test_missing_directory_root_is_an_absent_lookup(tmp_path: Path) -> None:
    assert contained_directory(tmp_path / "missing", "nested") is None
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("operation", ["resolve", "stat"])
@pytest.mark.parametrize("error_type", [PermissionError, OSError])
def test_regular_file_lookup_handles_filesystem_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    error_type: type[OSError],
) -> None:
    source = tmp_path / "result.md"
    source.write_bytes(b"retained")
    original = getattr(Path, operation)

    def inspect(path: Path, *args: object, **kwargs: object) -> object:
        if path == source:
            raise error_type("injected lookup failure")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, operation, inspect)
        if error_type is PermissionError:
            with pytest.raises(PermissionError, match="injected"):
                contained_regular_file(tmp_path, source.name)
        else:
            assert contained_regular_file(tmp_path, source.name) is None

    assert source.read_bytes() == b"retained"


@pytest.mark.parametrize(
    "race", ["replace-entry", "remove-entry", "extra-link", "rename-parent"]
)
def test_publication_rejects_races_without_removing_a_peer_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, race: str
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    parent = root / "nested"
    parent.mkdir()
    moved = root / "moved"
    source = tmp_path / "private.md"
    source.write_bytes(b"private")
    destination = parent / "result.md"
    original_link = os.link

    def publish(src: Path, dst: str, dst_dir_fd: int, follow_symlinks: bool) -> None:
        original_link(src, dst, dst_dir_fd=dst_dir_fd, follow_symlinks=follow_symlinks)
        if race == "rename-parent":
            parent.rename(moved)
            parent.mkdir()
        elif race == "extra-link":
            original_link(source, tmp_path / "peer-link.md")
        else:
            destination.unlink()
            if race == "replace-entry":
                destination.write_bytes(b"peer")

    with monkeypatch.context() as patch:
        patch.setattr(os, "link", publish)
        with pytest.raises((ValueError, FileNotFoundError)):
            replace_contained_file(root, "nested/result.md", source)

    if race == "replace-entry":
        assert destination.read_bytes() == b"peer"
    else:
        assert not destination.exists()
    if race == "rename-parent":
        assert not (moved / "result.md").exists()
    else:
        assert source.read_bytes() == b"private"


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"matched": False, "path": Path("result.md")}, "unmatched history read"),
        ({"matched": False, "error": "missing"}, "unmatched history read"),
        ({"matched": True, "path": Path("result.md")}, "requires a path and payload"),
        ({"matched": True, "payload": b"result"}, "requires a path and payload"),
        (
            {"matched": True, "error": "failed", "payload": b"result"},
            "failed history read",
        ),
    ],
)
def test_history_results_reject_ambiguous_success_or_failure(
    fields: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        TerminalHistoryRead(**fields)


def test_stage_task_provider_cannot_be_blank() -> None:
    with pytest.raises(ValueError, match="provider cannot be blank"):
        StageTaskSpec("draft", "executor", provider="  ")
