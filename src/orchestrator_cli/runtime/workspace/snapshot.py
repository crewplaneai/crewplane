from __future__ import annotations

import hashlib
import os
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from orchestrator_cli.core.file_hashing import FILE_HASH_CHUNK_BYTES
from orchestrator_cli.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSourceSnapshot,
)
from orchestrator_cli.core.workspace_cache import workspace_cache_root

from .git import GitCommand, sanitized_git_env


class Digest(Protocol):
    def update(self, data: bytes) -> None: ...


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
    workspace_path = run_root / slug
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


def workspace_run_root(
    plan: PreflightExecutionPlan,
    source: WorkspaceSourceSnapshot,
    family: str,
) -> Path:
    cache_root = workspace_cache_root(runtime_workspace_cache_root(plan))
    ensure_owner_private_dir(cache_root)
    family_root = cache_root / family
    ensure_owner_private_dir(family_root)
    repository_root = family_root / source.repository_id
    ensure_owner_private_dir(repository_root)
    run_root = repository_root / plan.run_key_name
    ensure_owner_private_dir(run_root)
    return run_root


def materialize_snapshot(
    source: WorkspaceSourceSnapshot,
    checkout_root: Path,
    index_path: Path,
) -> None:
    env = runtime_git_env(index_path)
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
        with TemporaryDirectory(prefix="orchestrator-cli-index-") as index_dir:
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


def snapshot_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for relative_path, entry_digest in snapshot_entries(root).items():
        digest.update(f"{relative_path}\0{entry_digest}\0".encode())
    return digest.hexdigest()


def snapshot_entries(root: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for current_root, dir_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_root)
        dir_names.sort()
        file_names.sort()
        for dir_name in tuple(dir_names):
            path = current / dir_name
            if path.is_symlink():
                entries[path.relative_to(root).as_posix()] = snapshot_entry_digest(
                    root,
                    path,
                    "symlink-dir",
                    os.readlink(path).encode("utf-8"),
                )
                dir_names.remove(dir_name)
                continue
            entries[path.relative_to(root).as_posix()] = snapshot_entry_digest(
                root,
                path,
                "dir",
                b"",
            )
        for file_name in file_names:
            path = current / file_name
            if path.is_symlink():
                entries[path.relative_to(root).as_posix()] = snapshot_entry_digest(
                    root,
                    path,
                    "symlink",
                    os.readlink(path).encode("utf-8"),
                )
                continue
            entries[path.relative_to(root).as_posix()] = snapshot_entry_digest(
                root,
                path,
                "file",
                None,
            )
    return dict(sorted(entries.items()))


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


def snapshot_entry_digest(
    root: Path,
    path: Path,
    kind: str,
    payload: bytes | None,
) -> str:
    digest = hashlib.sha256()
    digest_path(digest, root, path, kind)
    if payload is None:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(FILE_HASH_CHUNK_BYTES), b""):
                digest.update(chunk)
    else:
        digest.update(payload)
    return digest.hexdigest()


def digest_path(
    digest: Digest,
    root: Path,
    path: Path,
    kind: str,
) -> None:
    relative = path.relative_to(root).as_posix()
    mode = stat.S_IMODE(path.lstat().st_mode)
    digest.update(f"{kind}\0{relative}\0{mode:o}\0".encode())


def remove_workspace_path(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        path.unlink(missing_ok=True)
        return
    for current_root, dir_names, file_names in os.walk(path, topdown=False):
        del file_names
        current = Path(current_root)
        for dir_name in dir_names:
            dir_path = current / dir_name
            if not dir_path.is_symlink():
                dir_path.chmod(0o700)
        current.chmod(0o700)
    shutil.rmtree(path)


def ensure_owner_private_dir(path: Path) -> None:
    if path.exists() and not path.is_dir():
        raise RuntimeError(
            f"Workspace cache path is not a directory: {path.as_posix()}"
        )
    if path.is_symlink():
        raise RuntimeError(
            f"Workspace cache path must not be a symlink: {path.as_posix()}"
        )
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def runtime_workspace_cache_root(plan: PreflightExecutionPlan) -> str | None:
    workspace = plan.runtime_config_snapshot.get("workspace")
    if not isinstance(workspace, dict):
        return None
    value = workspace.get("cache_root")
    return value if isinstance(value, str) else None


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
