from __future__ import annotations

import stat
from pathlib import Path

from crewplane.core.preflight.models import WorkspaceSourceSnapshot

from .cleanup import registered_worktree_paths
from .types import WorktreeCaptureRequest


def verify_capture_layout(request: WorktreeCaptureRequest) -> None:
    require_real_capture_directory(
        request.workspace_path,
        "Workspace capture root",
    )
    if request.checkout_root != request.workspace_path / "checkout":
        raise RuntimeError("Workspace capture checkout path is not under its root.")
    require_real_capture_directory(
        request.checkout_root,
        "Workspace capture checkout",
    )


def require_real_capture_directory(path: Path, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise RuntimeError(f"{label} is missing: {path.as_posix()}.") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise RuntimeError(
            f"{label} must be a real directory and not a symlink: {path.as_posix()}."
        )


def require_regular_worktree_git_file(checkout_root: Path) -> Path:
    git_file = checkout_root / ".git"
    try:
        mode = git_file.lstat().st_mode
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Workspace capture requires a valid worktree .git file."
        ) from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise RuntimeError("Workspace capture requires a valid worktree .git file.")
    return git_file


def verify_worktree_git_metadata_identity(git_file: Path, git_dir: Path) -> None:
    marker_target = parse_worktree_gitdir_marker(git_file)
    if marker_target != git_dir:
        raise RuntimeError("Workspace capture .git file does not match Git dir.")
    backlink = parse_worktree_gitdir_backlink(git_dir)
    if backlink != git_file.resolve(strict=False):
        raise RuntimeError("Workspace capture Git dir does not belong to checkout.")


def parse_worktree_gitdir_marker(git_file: Path) -> Path:
    marker = "gitdir:"
    content = git_file.read_text(encoding="utf-8", errors="replace").strip()
    if not content.startswith(marker):
        raise RuntimeError("Workspace capture found an invalid worktree .git file.")
    raw_path = content[len(marker) :].strip()
    if not raw_path:
        raise RuntimeError("Workspace capture found an empty worktree Git dir.")
    return _resolve_git_metadata_path(raw_path, git_file.parent)


def parse_worktree_gitdir_backlink(git_dir: Path) -> Path:
    gitdir_file = git_dir / "gitdir"
    try:
        mode = gitdir_file.lstat().st_mode
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Workspace capture Git dir is missing its checkout pointer."
        ) from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise RuntimeError("Workspace capture Git dir checkout pointer is invalid.")
    raw_path = gitdir_file.read_text(encoding="utf-8", errors="replace").strip()
    if not raw_path:
        raise RuntimeError("Workspace capture Git dir checkout pointer is empty.")
    return _resolve_git_metadata_path(raw_path, git_dir)


def worktree_is_registered(
    source: WorkspaceSourceSnapshot,
    checkout_root: Path,
) -> bool:
    expected = checkout_root.resolve(strict=False)
    return expected in registered_worktree_paths(Path(source.common_git_dir))


def _resolve_git_metadata_path(raw_path: str, relative_to: Path) -> Path:
    target = Path(raw_path)
    if not target.is_absolute():
        target = relative_to / target
    return target.resolve(strict=False)
