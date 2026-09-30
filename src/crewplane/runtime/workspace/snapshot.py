from __future__ import annotations

import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from crewplane.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSourceSnapshot,
)

from .filesystem import (
    ensure_owner_private_dir,
    remove_workspace_path,
    workspace_invocation_path,
    workspace_run_root,
)
from .git import GitCommand, sanitized_git_env

SNAPSHOT_DRIFT_PATH_LIMIT = 20


@dataclass(frozen=True)
class SnapshotDriftSummary:
    changed_path_count: int
    changed_paths: tuple[str, ...]
    changed_paths_truncated: bool


def create_snapshot_workspace(
    plan: PreflightExecutionPlan,
    slug: str,
    source: WorkspaceSourceSnapshot,
) -> Path:
    run_root = workspace_run_root(plan, source, "snapshots")
    workspace_path = workspace_invocation_path(run_root, slug)
    if workspace_path.exists() or workspace_path.is_symlink():
        raise RuntimeError(
            f"Workspace path already exists: {workspace_path.as_posix()}"
        )
    ensure_owner_private_dir(run_root)
    workspace_path.mkdir(mode=0o700)
    workspace_path.chmod(0o700)
    checkout_root = workspace_path / "checkout"
    checkout_root.mkdir(mode=0o700)
    checkout_root.chmod(0o700)
    source_top_level = Path(source.git_top_level)
    if not source_top_level.is_absolute():
        raise RuntimeError("Workspace source snapshot has a non-absolute Git root.")
    return workspace_path


def materialize_snapshot(
    source: WorkspaceSourceSnapshot,
    checkout_root: Path,
    index_path: Path,
) -> None:
    env = runtime_git_env(index_path)
    # Resolve checkout attributes from the snapshot and its private index.
    env["GIT_WORK_TREE"] = checkout_root.as_posix()
    git_top_level = Path(source.git_top_level)
    run_git(git_top_level, env, "read-tree", source.run_base_commit)
    if source.project_root_relative_path == ".":
        run_git(
            git_top_level,
            env,
            "checkout-index",
            "-a",
            f"--prefix={checkout_root.as_posix()}/",
        )
        return
    project_paths = snapshot_project_paths(source, git_top_level, env)
    run_git_with_input(
        git_top_level,
        env,
        project_paths,
        "checkout-index",
        "-z",
        "--stdin",
        f"--prefix={checkout_root.as_posix()}/",
    )
    project_checkout_root = checkout_root / source.project_root_relative_path
    project_checkout_root.mkdir(mode=0o700, parents=True, exist_ok=True)


def snapshot_retry_reset(
    source: WorkspaceSourceSnapshot,
    checkout_root: Path,
) -> Callable[[], None]:
    workspace_path = checkout_root.parent
    workspace_identity = workspace_directory_identity(workspace_path)

    def reset() -> None:
        reset_snapshot_checkout(source, checkout_root, workspace_identity)

    return reset


def reset_snapshot_checkout(
    source: WorkspaceSourceSnapshot,
    checkout_root: Path,
    workspace_identity: tuple[int, int],
) -> None:
    try:
        workspace_path = checkout_root.parent
        if workspace_directory_identity(workspace_path) != workspace_identity:
            raise RuntimeError(
                "Snapshot retry reset workspace directory changed before cleanup."
            )
        remove_workspace_path(checkout_root)
        checkout_root.mkdir(mode=0o700)
        checkout_root.chmod(0o700)
        with TemporaryDirectory(prefix="crewplane-index-") as index_dir:
            materialize_snapshot(
                source,
                checkout_root,
                Path(index_dir) / "snapshot.index",
            )
    except Exception as exc:
        raise RuntimeError("Snapshot workspace retry reset failed.") from exc


def workspace_directory_identity(path: Path) -> tuple[int, int]:
    try:
        stat_result = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Managed workspace directory is missing: {path.as_posix()}"
        ) from exc
    mode = stat_result.st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise RuntimeError(
            f"Managed workspace path is not a real directory: {path.as_posix()}"
        )
    return stat_result.st_dev, stat_result.st_ino


def snapshot_drift_summary(
    initial_entries: dict[str, str],
    current_entries: dict[str, str],
) -> SnapshotDriftSummary:
    changed_paths = tuple(
        sorted(
            path
            for path in set(initial_entries) | set(current_entries)
            if initial_entries.get(path) != current_entries.get(path)
        )
    )
    return SnapshotDriftSummary(
        changed_path_count=len(changed_paths),
        changed_paths=changed_paths[:SNAPSHOT_DRIFT_PATH_LIMIT],
        changed_paths_truncated=len(changed_paths) > SNAPSHOT_DRIFT_PATH_LIMIT,
    )


def runtime_git_env(index_path: Path) -> dict[str, str]:
    return sanitized_git_env(index_path)


def run_git(git_top_level: Path, env: dict[str, str], *args: str) -> None:
    GitCommand(cwd=git_top_level, env=env).run("--no-optional-locks", *args)


def run_git_with_input(
    git_top_level: Path,
    env: dict[str, str],
    input_data: bytes,
    *args: str,
) -> None:
    GitCommand(cwd=git_top_level, env=env).run_with_input(
        input_data,
        "--no-optional-locks",
        *args,
    )


def snapshot_project_paths(
    source: WorkspaceSourceSnapshot,
    git_top_level: Path,
    env: dict[str, str],
) -> bytes:
    result = GitCommand(cwd=git_top_level, env=env).run(
        "--literal-pathspecs",
        "--no-optional-locks",
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        "--name-only",
        source.run_base_commit,
        "--",
        source.project_root_relative_path,
    )
    return result.stdout
