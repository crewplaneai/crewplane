from __future__ import annotations

import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..git import GitCommand, git, git_error
from ..locks import git_metadata_lock
from .head import prove_detached_head, reject_attached_head_after_safe_detachment
from .inspection import changed_paths
from .policy import (
    reject_common_git_policy_drift,
    reject_worktree_git_policy_drift,
)
from .protected_refs import ProtectedRefSnapshot, reject_protected_ref_drift
from .types import WorktreeCaptureRequest

RETRY_RESET_GIT_TIMEOUT_SECONDS = 30.0


def worktree_retry_reset(
    capture_request: WorktreeCaptureRequest,
    cancel_requested: Callable[[], bool] | None = None,
) -> Callable[[], None]:
    def reset() -> None:
        reset_worktree_attempt(
            capture_request.checkout_root,
            capture_request.source_ref.source_commit,
            Path(capture_request.source.git_top_level),
            Path(capture_request.source.common_git_dir),
            capture_request.git_dir,
            capture_request.protected_refs,
            cancel_requested,
        )

    return reset


def reset_worktree_attempt(
    checkout_root: Path,
    source_commit: str,
    repo_root: Path,
    common_git_dir: Path,
    expected_git_dir: Path,
    protected_refs: ProtectedRefSnapshot,
    cancel_requested: Callable[[], bool] | None = None,
) -> None:
    try:
        with git_metadata_lock(common_git_dir, cancel_requested):
            _raise_if_cancelled(cancel_requested)
            reject_common_git_policy_drift(repo_root, common_git_dir)
            _raise_if_cancelled(cancel_requested)
            git_dir = _verified_worktree_git_dir(
                checkout_root,
                common_git_dir,
                expected_git_dir,
            )
            reject_protected_ref_drift(checkout_root, protected_refs)
            command = git(
                checkout_root,
                timeout_seconds=RETRY_RESET_GIT_TIMEOUT_SECONDS,
            )
            reject_attached_head_after_safe_detachment(checkout_root, command)
            prove_detached_head(checkout_root, source_commit, command)
            _clear_worktree_policy_files(git_dir)
            _raise_if_cancelled(cancel_requested)
            # A hard reset preserves skip-worktree flags in the existing index.
            command.run("read-tree", "--empty")
            command.run("reset", "--hard", source_commit)
            _raise_if_cancelled(cancel_requested)
            command.run("clean", "-dffx")
            _raise_if_cancelled(cancel_requested)
            _clear_worktree_policy_files(git_dir)
            reject_common_git_policy_drift(repo_root, common_git_dir)
            _raise_if_cancelled(cancel_requested)
            _verify_reset_state(checkout_root, source_commit, cancel_requested)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Workspace retry reset failed: {git_error(exc)}") from exc


def reset_reusable_worktree_checkout(
    checkout_root: Path,
    source_commit: str,
    repo_root: Path,
    common_git_dir: Path,
    expected_git_dir: Path,
) -> None:
    try:
        reject_common_git_policy_drift(repo_root, common_git_dir)
        git_dir = _verified_worktree_git_dir(
            checkout_root,
            common_git_dir,
            expected_git_dir,
        )
        command = git(
            checkout_root,
            timeout_seconds=RETRY_RESET_GIT_TIMEOUT_SECONDS,
        )
        reject_attached_head_after_safe_detachment(checkout_root, command)
        prove_detached_head(checkout_root, source_commit, command)
        _clear_worktree_policy_files(git_dir)
        command.run("read-tree", "--empty")
        command.run("reset", "--hard", source_commit)
        command.run("clean", "-dffx")
        _clear_worktree_policy_files(git_dir)
        reject_common_git_policy_drift(repo_root, common_git_dir)
        _verify_reset_state(checkout_root, source_commit)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"Reusable workspace reset failed: {git_error(exc)}"
        ) from exc


def _verify_reset_state(
    checkout_root: Path,
    source_commit: str,
    cancel_requested: Callable[[], bool] | None = None,
) -> None:
    command = git(checkout_root, timeout_seconds=RETRY_RESET_GIT_TIMEOUT_SECONDS)
    _raise_if_cancelled(cancel_requested)
    _verify_source_commit(command, source_commit)
    _raise_if_cancelled(cancel_requested)
    _verify_detached_head(command)
    _raise_if_cancelled(cancel_requested)
    reject_worktree_git_policy_drift(checkout_root)
    _raise_if_cancelled(cancel_requested)
    _verify_clean_checkout(checkout_root)


def _verify_source_commit(command: GitCommand, source_commit: str) -> None:
    head = command.text("rev-parse", "HEAD^{commit}")
    if head != source_commit:
        raise RuntimeError("Workspace retry reset did not restore the source commit.")


def _verify_detached_head(command: GitCommand) -> None:
    if command.text("branch", "--show-current"):
        raise RuntimeError("Workspace retry reset did not restore detached HEAD.")


def _verify_clean_checkout(checkout_root: Path) -> None:
    if changed_paths(checkout_root):
        raise RuntimeError("Workspace retry reset left changed paths behind.")


def _raise_if_cancelled(cancel_requested: Callable[[], bool] | None) -> None:
    if cancel_requested is not None and cancel_requested():
        raise RuntimeError("Workspace retry reset was cancelled.")


def _clear_worktree_policy_files(git_dir: Path) -> None:
    _require_real_directory(git_dir, "Workspace retry reset Git dir")
    _unlink_policy_file(git_dir / "config.worktree")
    info_dir = git_dir / "info"
    if not _optional_real_directory(info_dir, "Workspace retry reset Git info dir"):
        return
    for name in ("attributes", "exclude"):
        _unlink_policy_file(info_dir / name)


def _require_real_directory(path: Path, description: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise RuntimeError(f"{description} is missing.") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise RuntimeError(f"{description} must be a real directory.")


def _optional_real_directory(path: Path, description: str) -> bool:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise RuntimeError(f"{description} must be a real directory.")
    return True


def _unlink_policy_file(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise RuntimeError(
            f"Workspace retry reset policy file is not a real file: {path.name}"
        )
    path.unlink()


@dataclass(frozen=True)
class _WorktreeGitMetadata:
    git_file: Path
    raw_git_dir: Path
    git_dir: Path


def _verified_worktree_git_dir(
    checkout_root: Path,
    common_git_dir: Path,
    expected_git_dir: Path,
) -> Path:
    metadata = _worktree_git_metadata(checkout_root, common_git_dir)
    if metadata.git_dir != expected_git_dir.resolve(strict=False):
        raise RuntimeError("Workspace retry reset .git file does not match Git dir.")
    _reject_symlinked_git_dir(metadata.raw_git_dir, common_git_dir)
    backlink = _gitdir_backlink(metadata.git_dir)
    if backlink != metadata.git_file.resolve(strict=False):
        raise RuntimeError("Workspace retry reset Git dir does not belong to checkout.")
    return metadata.git_dir


def _worktree_git_metadata(
    checkout_root: Path,
    common_git_dir: Path,
) -> _WorktreeGitMetadata:
    git_file = _require_worktree_git_file(checkout_root)
    raw_git_dir = _read_worktree_git_dir(git_file)
    git_dir = raw_git_dir.resolve(strict=False)
    _require_git_dir_within_common_dir(git_dir, common_git_dir)
    return _WorktreeGitMetadata(
        git_file=git_file,
        raw_git_dir=raw_git_dir,
        git_dir=git_dir,
    )


def _require_worktree_git_file(checkout_root: Path) -> Path:
    git_file = checkout_root / ".git"
    try:
        mode = git_file.lstat().st_mode
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Workspace retry reset requires a valid worktree .git file."
        ) from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise RuntimeError("Workspace retry reset requires a valid worktree .git file.")
    return git_file


def _read_worktree_git_dir(git_file: Path) -> Path:
    marker = "gitdir:"
    content = git_file.read_text(encoding="utf-8", errors="replace").strip()
    if not content.startswith(marker):
        raise RuntimeError("Workspace retry reset found an invalid worktree .git file.")
    raw_path = content[len(marker) :].strip()
    if not raw_path:
        raise RuntimeError("Workspace retry reset found an empty worktree Git dir.")
    return _path_from_git_metadata(raw_path, git_file.parent)


def _require_git_dir_within_common_dir(
    git_dir: Path,
    common_git_dir: Path,
) -> None:
    if not git_dir.is_relative_to(common_git_dir.resolve(strict=False)):
        raise RuntimeError("Workspace retry reset Git dir escapes the common Git dir.")


def _path_from_git_metadata(raw_path: str, relative_to: Path) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return relative_to / path


def _reject_symlinked_git_dir(git_dir: Path, common_git_dir: Path) -> None:
    try:
        relative = git_dir.relative_to(common_git_dir)
    except ValueError:
        _reject_symlinked_path(git_dir)
        return
    current = common_git_dir
    for part in relative.parts:
        current /= part
        _reject_symlinked_path(current)


def _reject_symlinked_path(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise RuntimeError("Workspace retry reset Git metadata is missing.") from exc
    if stat.S_ISLNK(mode):
        raise RuntimeError("Workspace retry reset Git metadata must not be symlinked.")


def _gitdir_backlink(git_dir: Path) -> Path:
    gitdir_file = _require_gitdir_backlink_file(git_dir)
    raw_path = _read_gitdir_backlink(gitdir_file)
    return _path_from_git_metadata(raw_path, git_dir).resolve(strict=False)


def _require_gitdir_backlink_file(git_dir: Path) -> Path:
    gitdir_file = git_dir / "gitdir"
    try:
        mode = gitdir_file.lstat().st_mode
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Workspace retry reset Git dir is missing its checkout pointer."
        ) from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise RuntimeError("Workspace retry reset Git dir checkout pointer is invalid.")
    return gitdir_file


def _read_gitdir_backlink(gitdir_file: Path) -> str:
    raw_path = gitdir_file.read_text(encoding="utf-8", errors="replace").strip()
    if not raw_path:
        raise RuntimeError("Workspace retry reset Git dir checkout pointer is empty.")
    return raw_path
