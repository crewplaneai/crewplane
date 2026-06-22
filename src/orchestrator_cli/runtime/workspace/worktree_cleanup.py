from __future__ import annotations

import shutil
import stat
from pathlib import Path

from .git import GitCommand, sanitized_git_env
from .locks import git_metadata_lock

_GIT_WORKTREE_CLEANUP_TIMEOUT_SECONDS = 30.0


def worktree_disk_usage(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for current_root, dir_names, file_names in path.walk():
        dir_names.sort()
        for file_name in file_names:
            try:
                total += (current_root / file_name).lstat().st_size
            except OSError:
                continue
    return total


def remove_unknown_workspace_path(
    path: Path,
    expected_common_git_dir: Path | None = None,
) -> None:
    if path.is_symlink():
        raise RuntimeError(f"Workspace cleanup path must not be a symlink: {path}")
    if not path.exists():
        return
    if not path.is_dir():
        raise RuntimeError(f"Workspace cleanup path is not a directory: {path}")
    checkout_root = path / "checkout"
    if _checkout_has_git_admin_entry(checkout_root):
        common_git_dir = _verified_common_git_dir(
            checkout_root,
            expected_common_git_dir,
        )
        if common_git_dir is None:
            raise RuntimeError(
                "Refusing to raw-delete Git-looking workspace checkout because "
                "its Git metadata could not be verified: "
                f"{checkout_root.as_posix()}"
            )
        with git_metadata_lock(common_git_dir):
            if not _registered_worktree_path(common_git_dir, checkout_root):
                raise RuntimeError(
                    "Refusing to raw-delete Git-looking workspace checkout because "
                    "it is not registered in the verified common Git directory: "
                    f"{checkout_root.as_posix()}"
                )
            _remove_registered_worktree(common_git_dir, checkout_root)
    shutil.rmtree(path)


def _checkout_has_git_admin_entry(checkout_root: Path) -> bool:
    return (
        checkout_root.is_dir()
        and not checkout_root.is_symlink()
        and _git_admin_entry_exists(checkout_root / ".git")
    )


def _git_admin_entry_exists(git_entry: Path) -> bool:
    try:
        git_entry.lstat()
    except FileNotFoundError:
        return False
    return True


def _verified_common_git_dir(
    checkout_root: Path,
    expected_common_git_dir: Path | None,
) -> Path | None:
    git_file = _valid_git_file(checkout_root)
    if git_file is None:
        return None
    git_dir = _gitdir_marker_target(git_file)
    if git_dir is None:
        return None
    common_git_dir = _common_git_dir_for_worktree_git_dir(
        git_dir,
        expected_common_git_dir,
    )
    if common_git_dir is None:
        return None
    if not _valid_common_git_dir(common_git_dir):
        return None
    if not git_dir.is_relative_to(common_git_dir):
        return None
    if _path_has_symlink_or_missing_component(git_dir, common_git_dir):
        return None
    backlink = _gitdir_backlink(git_dir)
    if backlink != git_file.resolve(strict=False):
        return None
    return common_git_dir


def _valid_common_git_dir(common_git_dir: Path) -> bool:
    try:
        mode = common_git_dir.lstat().st_mode
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(mode) and not stat.S_ISLNK(mode)


def _common_git_dir_for_worktree_git_dir(
    git_dir: Path,
    expected_common_git_dir: Path | None,
) -> Path | None:
    if expected_common_git_dir is not None:
        return expected_common_git_dir.resolve(strict=False)
    if git_dir.parent.name != "worktrees":
        return None
    return git_dir.parent.parent.resolve(strict=False)


def _valid_git_file(checkout_root: Path) -> Path | None:
    git_file = checkout_root / ".git"
    try:
        mode = git_file.lstat().st_mode
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        return None
    return git_file


def _gitdir_marker_target(git_file: Path) -> Path | None:
    marker = "gitdir:"
    try:
        content = git_file.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not content.startswith(marker):
        return None
    raw_path = content[len(marker) :].strip()
    if not raw_path:
        return None
    git_dir = Path(raw_path)
    if not git_dir.is_absolute():
        git_dir = git_file.parent / git_dir
    return git_dir.resolve(strict=False)


def _gitdir_backlink(git_dir: Path) -> Path | None:
    gitdir_file = git_dir / "gitdir"
    try:
        mode = gitdir_file.lstat().st_mode
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        return None
    try:
        raw_path = gitdir_file.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not raw_path:
        return None
    target = Path(raw_path)
    if not target.is_absolute():
        target = git_dir / target
    return target.resolve(strict=False)


def _path_has_symlink_or_missing_component(path: Path, root: Path) -> bool:
    current = root
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    for part in relative.parts:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return True
        if stat.S_ISLNK(mode):
            return True
    return False


def _registered_worktree_path(common_git_dir: Path, checkout_root: Path) -> bool:
    result = _common_git_command(common_git_dir).run(
        "--git-dir",
        common_git_dir.as_posix(),
        "worktree",
        "list",
        "--porcelain",
    )
    records = result.stdout.decode("utf-8", errors="replace").splitlines()
    expected = checkout_root.resolve(strict=False)
    return any(
        record.startswith("worktree ")
        and Path(record.removeprefix("worktree ")).resolve(strict=False) == expected
        for record in records
    )


def _remove_registered_worktree(common_git_dir: Path, checkout_root: Path) -> None:
    _common_git_command(common_git_dir).run(
        "--git-dir",
        common_git_dir.as_posix(),
        "worktree",
        "remove",
        "--force",
        "--force",
        checkout_root.as_posix(),
    )


def _common_git_command(common_git_dir: Path) -> GitCommand:
    return GitCommand(
        cwd=common_git_dir.parent,
        env=sanitized_git_env(),
        timeout_seconds=_GIT_WORKTREE_CLEANUP_TIMEOUT_SECONDS,
    )
