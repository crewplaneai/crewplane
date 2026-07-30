from __future__ import annotations

import hashlib
import math
import os
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

from crewplane.core.file_hashing import FILE_HASH_CHUNK_BYTES
from crewplane.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSourceSnapshot,
)
from crewplane.core.workspace.cache import workspace_cache_root

from .git import GitCommand, sanitized_git_env

SNAPSHOT_DRIFT_PATH_LIMIT = 20
DEFAULT_SNAPSHOT_MAX_ENTRIES = 250_000
DEFAULT_SNAPSHOT_MAX_FILE_BYTES = 4 * 1024 * 1024 * 1024
DEFAULT_SNAPSHOT_MAX_ELAPSED_SECONDS = 30.0


@dataclass(frozen=True)
class SnapshotDriftSummary:
    changed_path_count: int
    changed_paths: tuple[str, ...]
    changed_paths_truncated: bool


class WorkspaceSnapshotError(RuntimeError):
    """Base error for bounded workspace snapshot failures."""


class WorkspaceSnapshotLimitError(WorkspaceSnapshotError):
    """Raised when a workspace snapshot exceeds a configured resource limit."""


class WorkspaceSnapshotEntryError(WorkspaceSnapshotError):
    """Raised when a workspace contains an unsupported entry type."""


class WorkspaceSnapshotRaceError(WorkspaceSnapshotError):
    """Raised when an entry disappears or changes type during snapshotting."""


class WorkspaceSnapshotCancelled(WorkspaceSnapshotError):
    """Raised when snapshot cancellation is requested."""


@dataclass(frozen=True)
class WorkspaceSnapshotPolicy:
    max_entries: int = DEFAULT_SNAPSHOT_MAX_ENTRIES
    max_file_bytes: int = DEFAULT_SNAPSHOT_MAX_FILE_BYTES
    max_elapsed_seconds: float = DEFAULT_SNAPSHOT_MAX_ELAPSED_SECONDS
    cancel_requested: Callable[[], bool] | None = None
    clock: Callable[[], float] = monotonic

    def __post_init__(self) -> None:
        if self.max_entries < 1:
            raise ValueError("Workspace snapshot max_entries must be positive.")
        if self.max_file_bytes < 0:
            raise ValueError("Workspace snapshot max_file_bytes must be nonnegative.")
        if not math.isfinite(self.max_elapsed_seconds) or self.max_elapsed_seconds <= 0:
            raise ValueError("Workspace snapshot max_elapsed_seconds must be positive.")


@dataclass
class _WorkspaceSnapshotBudget:
    policy: WorkspaceSnapshotPolicy
    started_at: float
    entry_count: int = 0
    file_bytes: int = 0


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


def snapshot_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for relative_path, entry_digest in snapshot_entries(root).items():
        digest.update(f"{relative_path}\0{entry_digest}\0".encode())
    return digest.hexdigest()


def snapshot_entries(
    root: Path,
    policy: WorkspaceSnapshotPolicy | None = None,
) -> dict[str, str]:
    resolved_policy = policy or WorkspaceSnapshotPolicy()
    budget = _WorkspaceSnapshotBudget(
        policy=resolved_policy,
        started_at=resolved_policy.clock(),
    )
    root_stat = _require_snapshot_directory(root)
    entries: dict[str, str] = {}
    root_descriptor = _open_snapshot_directory(root, root_stat, ".")
    try:
        _scan_snapshot_directory(budget, root_descriptor, "", entries)
    finally:
        os.close(root_descriptor)
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


def _snapshot_entry_digest(
    relative: str,
    entry_stat: os.stat_result,
    kind: str,
    payload: bytes,
) -> str:
    digest = hashlib.sha256()
    mode = stat.S_IMODE(entry_stat.st_mode)
    digest.update(f"{kind}\0{relative}\0{mode:o}\0".encode())
    digest.update(payload)
    return digest.hexdigest()


def _scan_snapshot_directory(
    budget: _WorkspaceSnapshotBudget,
    directory_descriptor: int,
    relative_parent: str,
    entries: dict[str, str],
) -> None:
    _check_snapshot_budget(budget)
    discovered: list[tuple[str, str, os.stat_result]] = []
    try:
        with os.scandir(directory_descriptor) as iterator:
            for entry in iterator:
                relative = (
                    f"{relative_parent}/{entry.name}" if relative_parent else entry.name
                )
                _count_snapshot_entry(budget, relative)
                discovered.append(
                    (
                        entry.name,
                        relative,
                        _snapshot_lstat_at(
                            directory_descriptor,
                            entry.name,
                            relative,
                        ),
                    )
                )
    except WorkspaceSnapshotError:
        raise
    except OSError as exc:
        location = relative_parent or "."
        raise WorkspaceSnapshotRaceError(
            f"Workspace snapshot directory changed while scanning: {location}"
        ) from exc

    for name, relative, entry_stat in sorted(discovered):
        mode = entry_stat.st_mode
        if stat.S_ISLNK(mode):
            entries[relative] = _snapshot_entry_digest(
                relative,
                entry_stat,
                "symlink",
                _read_snapshot_symlink_at(
                    directory_descriptor,
                    name,
                    relative,
                    entry_stat,
                ),
            )
            continue
        if stat.S_ISDIR(mode):
            child_descriptor = _open_snapshot_directory_at(
                directory_descriptor,
                name,
                entry_stat,
                relative,
            )
            try:
                opened_stat = os.fstat(child_descriptor)
                entries[relative] = _snapshot_entry_digest(
                    relative,
                    opened_stat,
                    "dir",
                    b"",
                )
                _scan_snapshot_directory(
                    budget,
                    child_descriptor,
                    relative,
                    entries,
                )
            finally:
                os.close(child_descriptor)
            continue
        if not stat.S_ISREG(mode):
            raise _unsupported_snapshot_entry(relative, mode)
        entries[relative] = _snapshot_regular_file_digest(
            budget,
            directory_descriptor,
            name,
            relative,
            entry_stat,
        )


def _snapshot_regular_file_digest(
    budget: _WorkspaceSnapshotBudget,
    directory_descriptor: int,
    name: str,
    relative: str,
    entry_stat: os.stat_result,
) -> str:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    except OSError as exc:
        raise WorkspaceSnapshotRaceError(
            f"Workspace snapshot entry changed before open: {relative}"
        ) from exc
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode) or (
            opened_stat.st_dev,
            opened_stat.st_ino,
        ) != (entry_stat.st_dev, entry_stat.st_ino):
            raise WorkspaceSnapshotRaceError(
                f"Workspace snapshot entry changed type or identity: {relative}"
            )
        _reserve_snapshot_file_bytes(budget, opened_stat.st_size, relative)
        digest = hashlib.sha256()
        mode = stat.S_IMODE(opened_stat.st_mode)
        digest.update(f"file\0{relative}\0{mode:o}\0".encode())
        bytes_read = 0
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            for chunk in iter(lambda: handle.read(FILE_HASH_CHUNK_BYTES), b""):
                _check_snapshot_budget(budget)
                bytes_read += len(chunk)
                if bytes_read > opened_stat.st_size:
                    raise WorkspaceSnapshotRaceError(
                        f"Workspace snapshot file grew while hashing: {relative}"
                    )
                digest.update(chunk)
        if bytes_read != opened_stat.st_size:
            raise WorkspaceSnapshotRaceError(
                f"Workspace snapshot file changed size while hashing: {relative}"
            )
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _require_snapshot_directory(root: Path) -> os.stat_result:
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise WorkspaceSnapshotRaceError(
            f"Workspace snapshot root is unavailable: {root}"
        ) from exc
    if not stat.S_ISDIR(root_stat.st_mode):
        raise WorkspaceSnapshotEntryError(
            f"Workspace snapshot root is not a directory: {root}"
        )
    return root_stat


def _snapshot_lstat_at(
    directory_descriptor: int,
    name: str,
    relative: str,
) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except OSError as exc:
        raise WorkspaceSnapshotRaceError(
            f"Workspace snapshot entry disappeared: {relative}"
        ) from exc


def _read_snapshot_symlink_at(
    directory_descriptor: int,
    name: str,
    relative: str,
    entry_stat: os.stat_result,
) -> bytes:
    try:
        target = os.readlink(name, dir_fd=directory_descriptor)
        current_stat = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise WorkspaceSnapshotRaceError(
            f"Workspace snapshot symlink changed: {relative}"
        ) from exc
    if not stat.S_ISLNK(current_stat.st_mode) or not _same_snapshot_entry(
        entry_stat,
        current_stat,
    ):
        raise WorkspaceSnapshotRaceError(
            f"Workspace snapshot symlink changed: {relative}"
        )
    return target.encode("utf-8")


def _open_snapshot_directory(
    path: Path,
    entry_stat: os.stat_result,
    relative: str,
) -> int:
    return _open_snapshot_directory_target(path, None, entry_stat, relative)


def _open_snapshot_directory_at(
    directory_descriptor: int,
    name: str,
    entry_stat: os.stat_result,
    relative: str,
) -> int:
    return _open_snapshot_directory_target(
        name,
        directory_descriptor,
        entry_stat,
        relative,
    )


def _open_snapshot_directory_target(
    target: str | Path,
    directory_descriptor: int | None,
    entry_stat: os.stat_result,
    relative: str,
) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(target, flags, dir_fd=directory_descriptor)
    except OSError as exc:
        raise WorkspaceSnapshotRaceError(
            f"Workspace snapshot directory changed before open: {relative}"
        ) from exc
    opened_stat = os.fstat(descriptor)
    if not stat.S_ISDIR(opened_stat.st_mode) or not _same_snapshot_entry(
        entry_stat,
        opened_stat,
    ):
        os.close(descriptor)
        raise WorkspaceSnapshotRaceError(
            f"Workspace snapshot directory changed type or identity: {relative}"
        )
    return descriptor


def _same_snapshot_entry(
    first: os.stat_result,
    second: os.stat_result,
) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _unsupported_snapshot_entry(
    relative: str,
    mode: int,
) -> WorkspaceSnapshotEntryError:
    return WorkspaceSnapshotEntryError(
        "Workspace snapshots support only directories, regular files, and "
        f"symlinks; rejected '{relative}' with mode {stat.S_IFMT(mode):#o}."
    )


def _count_snapshot_entry(
    budget: _WorkspaceSnapshotBudget,
    relative: str,
) -> None:
    _check_snapshot_budget(budget)
    budget.entry_count += 1
    if budget.entry_count > budget.policy.max_entries:
        raise WorkspaceSnapshotLimitError(
            "Workspace snapshot entry limit exceeded "
            f"at '{relative}' ({budget.policy.max_entries})."
        )


def _reserve_snapshot_file_bytes(
    budget: _WorkspaceSnapshotBudget,
    size_bytes: int,
    relative: str,
) -> None:
    budget.file_bytes += size_bytes
    if budget.file_bytes > budget.policy.max_file_bytes:
        raise WorkspaceSnapshotLimitError(
            "Workspace snapshot byte limit exceeded "
            f"at '{relative}' ({budget.policy.max_file_bytes})."
        )


def _check_snapshot_budget(budget: _WorkspaceSnapshotBudget) -> None:
    if budget.policy.cancel_requested is not None and budget.policy.cancel_requested():
        raise WorkspaceSnapshotCancelled("Workspace snapshot was cancelled.")
    elapsed = budget.policy.clock() - budget.started_at
    if elapsed > budget.policy.max_elapsed_seconds:
        raise WorkspaceSnapshotLimitError(
            "Workspace snapshot elapsed-time limit exceeded "
            f"({budget.policy.max_elapsed_seconds:g}s)."
        )


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
