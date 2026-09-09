from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.runtime.workspace.git import GitCommand
from crewplane.runtime.workspace.worktree.checkout_identity import (
    parse_worktree_gitdir_backlink,
    parse_worktree_gitdir_marker,
    require_real_capture_directory,
    require_regular_worktree_git_file,
    verify_worktree_git_metadata_identity,
)
from crewplane.runtime.workspace.worktree.cleanup import (
    registered_worktree_paths,
    remove_unknown_workspace_path,
    verify_registered_worktree_cleanup_path,
    worktree_disk_usage,
)
from tests.helpers import isolated_git as isolated_git_support
from tests.helpers.isolated_git import IsolatedGit
from tests.helpers.workspace_service import create_git_repo, run_git_text

isolated_git = isolated_git_support.isolated_git


@pytest.mark.parametrize(
    ("fallback", "output", "message"),
    [
        (False, b"\xff", "valid UTF-8"),
        (False, b"HEAD abc\0", "no worktree paths"),
        (False, b"worktree \0", "malformed"),
        (False, b"worktree /tmp/bad\npath\0", "malformed"),
        (False, b"worktree /tmp/bad\x7fpath\0", "malformed"),
        (True, b"\xff", "valid UTF-8"),
        (True, b"worktree /tmp/path\r\n", "control bytes"),
        (True, b"worktree /tmp/path\0", "control bytes"),
        (True, b"HEAD abc\n", "record is malformed"),
        (True, b"", "no worktree paths"),
        (True, b"worktree \n", "quoted, escaped, or malformed"),
        (True, b'worktree "/tmp/path"\n', "quoted, escaped, or malformed"),
        (True, b"worktree /tmp/bad\\path\n", "quoted, escaped, or malformed"),
        (True, b"worktree /tmp/bad\tpath\n", "quoted, escaped, or malformed"),
    ],
)
def test_cleanup_refuses_malformed_git_worktree_records(
    tmp_path: Path, fallback: bool, output: bytes, message: str
) -> None:
    result = subprocess.CompletedProcess(["git"], 0, output, b"")
    responses = (
        [subprocess.CalledProcessError(129, ["git"]), result] if fallback else [result]
    )
    with (
        patch.object(GitCommand, "run", side_effect=responses) as command,
        pytest.raises(RuntimeError, match=message),
    ):
        registered_worktree_paths(tmp_path)
    assert command.call_count == (2 if fallback else 1)
    assert command.call_args_list[0].args[-1] == "-z"
    if fallback:
        assert command.call_args.args[-1] == "--porcelain"


def test_cleanup_does_not_fallback_after_git_repository_failure(tmp_path: Path) -> None:
    error = subprocess.CalledProcessError(128, ["git"], stderr=b"not a repository")
    with (
        patch.object(GitCommand, "run", side_effect=error) as command,
        pytest.raises(subprocess.CalledProcessError) as raised,
    ):
        registered_worktree_paths(tmp_path)
    assert raised.value is error
    command.assert_called_once()


@pytest.mark.parametrize("fallback", [False, True])
def test_cleanup_preserves_spaces_in_registered_worktree_paths(
    tmp_path: Path, fallback: bool
) -> None:
    checkout = tmp_path / "checkout with spaces"
    separator = "\n" if fallback else "\0"
    output = f"worktree {checkout}{separator}HEAD abc{separator}{separator}".encode()
    result = subprocess.CompletedProcess(["git"], 0, output, b"")
    responses = (
        [subprocess.CalledProcessError(129, ["git"]), result] if fallback else [result]
    )
    with patch.object(GitCommand, "run", side_effect=responses):
        assert registered_worktree_paths(tmp_path) == (checkout,)


@pytest.mark.parametrize("kind", ["missing", "file", "symlink"])
def test_capture_requires_real_directories(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "capture"
    if kind == "file":
        root.write_text("keep", encoding="utf-8")
    elif kind == "symlink":
        root.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(RuntimeError, match="missing|real directory"):
        require_real_capture_directory(root, "Capture")


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink"])
def test_capture_requires_regular_git_marker(tmp_path: Path, kind: str) -> None:
    marker = tmp_path / ".git"
    if kind == "directory":
        marker.mkdir()
    elif kind == "symlink":
        marker.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(RuntimeError, match="valid worktree .git file"):
        require_regular_worktree_git_file(tmp_path)


@pytest.mark.parametrize("content", ["", "not a git marker", "gitdir:  \n"])
def test_capture_rejects_invalid_git_marker(tmp_path: Path, content: str) -> None:
    marker = tmp_path / ".git"
    marker.write_text(content, encoding="utf-8")
    with pytest.raises(RuntimeError, match="invalid worktree|empty worktree"):
        parse_worktree_gitdir_marker(marker)


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink", "empty"])
def test_capture_rejects_invalid_git_backlink(tmp_path: Path, kind: str) -> None:
    backlink = tmp_path / "gitdir"
    if kind == "directory":
        backlink.mkdir()
    elif kind == "symlink":
        backlink.symlink_to(tmp_path / "elsewhere")
    elif kind == "empty":
        backlink.write_text(" \n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="checkout pointer"):
        parse_worktree_gitdir_backlink(tmp_path)


def test_capture_resolves_relative_git_identity_and_rejects_retargeting(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    git_dir = tmp_path / "metadata"
    git_dir.mkdir()
    marker = checkout / ".git"
    marker.write_text("gitdir: ../metadata\n", encoding="utf-8")
    backlink = git_dir / "gitdir"
    backlink.write_text("../checkout/.git\n", encoding="utf-8")
    verify_worktree_git_metadata_identity(marker, git_dir)
    with pytest.raises(RuntimeError, match="does not match Git dir"):
        verify_worktree_git_metadata_identity(marker, tmp_path / "other")
    backlink.write_text("../other/.git\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="does not belong to checkout"):
        verify_worktree_git_metadata_identity(marker, git_dir)


@pytest.mark.parametrize("kind", ["symlink", "file", "lost-metadata"])
def test_cleanup_preserves_unowned_workspace_paths(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "workspace"
    expected_git_dir = None
    if kind == "symlink":
        path.symlink_to(tmp_path, target_is_directory=True)
    elif kind == "file":
        path.write_text("keep", encoding="utf-8")
    else:
        path.mkdir()
        expected_git_dir = tmp_path / "metadata"
    with pytest.raises(
        RuntimeError, match="symlink|not a directory|persisted identity"
    ):
        remove_unknown_workspace_path(path, expected_worktree_git_dir=expected_git_dir)
    assert path.exists()


@pytest.mark.parametrize(
    "damage",
    [
        "missing-marker",
        "directory-marker",
        "symlink-marker",
        "bad-marker",
        "empty-marker",
        "outside-git-dir",
        "missing-git-dir",
        "missing-common",
        "file-common",
        "missing-backlink",
        "directory-backlink",
        "symlink-backlink",
        "empty-backlink",
        "wrong-backlink",
    ],
)
def test_cleanup_rejects_damaged_git_ownership_without_deleting_workspace(
    tmp_path: Path, isolated_git: IsolatedGit, damage: str
) -> None:
    del isolated_git
    repo = create_git_repo(tmp_path)
    workspace = tmp_path / "workspace"
    checkout = workspace / "checkout"
    workspace.mkdir()
    run_git_text(repo, "worktree", "add", "--detach", str(checkout))
    common = repo / ".git"
    marker = checkout / ".git"
    git_dir = parse_worktree_gitdir_marker(marker)
    backlink = git_dir / "gitdir"
    verify_registered_worktree_cleanup_path(workspace, common, git_dir)
    sentinel = checkout / "user-file"
    sentinel.write_text("keep", encoding="utf-8")
    if damage.endswith("marker"):
        marker.unlink()
        if damage == "directory-marker":
            marker.mkdir()
        elif damage == "symlink-marker":
            marker.symlink_to(backlink)
        elif damage == "bad-marker":
            marker.write_text("not gitdir", encoding="utf-8")
        elif damage == "empty-marker":
            marker.write_text("gitdir: \n", encoding="utf-8")
    elif damage.endswith("backlink"):
        backlink.unlink()
        if damage == "directory-backlink":
            backlink.mkdir()
        elif damage == "symlink-backlink":
            backlink.symlink_to(marker)
        elif damage == "empty-backlink":
            backlink.write_text("\n", encoding="utf-8")
        elif damage == "wrong-backlink":
            backlink.write_text(str(tmp_path / "unowned"), encoding="utf-8")
    elif damage == "outside-git-dir":
        marker.write_text(f"gitdir: {tmp_path / 'outside'}", encoding="utf-8")
    elif damage == "missing-git-dir":
        marker.write_text(f"gitdir: {common / 'missing'}", encoding="utf-8")
    else:
        common = tmp_path / "unavailable-common"
        if damage == "file-common":
            common.write_text("not a directory", encoding="utf-8")
    with pytest.raises(RuntimeError, match="expected repository"):
        verify_registered_worktree_cleanup_path(workspace, common, git_dir)
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_cleanup_handles_missing_and_unregistered_plain_workspaces(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workspace"
    assert worktree_disk_usage(path) == 0
    remove_unknown_workspace_path(path)
    path.mkdir()
    (path / "file").write_bytes(b"contents")
    assert worktree_disk_usage(path) == 8
    remove_unknown_workspace_path(path)
    assert not path.exists()
