from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from crewplane.core.config import Settings
from crewplane.core.workspace.cache import workspace_cache_root

from .filesystem_policy import existing_probe_parent
from .git_source import GitSourceContext, git_zero_records
from .repo_policy import is_reserved_source_path
from .source_types import WorkspacePolicyBuilder

DEFAULT_DISK_WARN_FREE_BYTES = 2 * 1024 * 1024 * 1024


@dataclass(frozen=True)
class DiskSpaceEstimate:
    checkout_size_bytes: int
    remaining_free_bytes: int


def warn_storage_pressure(
    settings: Settings,
    git_context: GitSourceContext,
    estimate_full_repository: bool,
    builder: WorkspacePolicyBuilder,
) -> None:
    cache_root = workspace_cache_root(settings.workspace.cache_root)
    probe_root = existing_probe_parent(cache_root)
    try:
        usage = shutil.disk_usage(probe_root)
    except OSError:
        return
    disk = settings.workspace.disk
    estimate = disk_space_estimate(usage.free, git_context, estimate_full_repository)
    if (
        disk.fail_free_bytes is not None
        and estimate.remaining_free_bytes < disk.fail_free_bytes
    ):
        builder.errors.append(
            "Workspace source policy failed: cache filesystem has "
            f"{usage.free} free byte(s) near {cache_root.as_posix()}; estimated "
            f"checkout size is {estimate.checkout_size_bytes} byte(s), leaving "
            f"{estimate.remaining_free_bytes} byte(s), below "
            f"settings.workspace.disk.fail_free_bytes={disk.fail_free_bytes}."
        )
    if disk.warn_free_bytes is not None:
        if estimate.remaining_free_bytes < disk.warn_free_bytes:
            builder.warnings.append(
                "Workspace source policy warning: cache filesystem has "
                f"{usage.free} free byte(s) near {cache_root.as_posix()}; estimated "
                f"checkout size is {estimate.checkout_size_bytes} byte(s), leaving "
                f"{estimate.remaining_free_bytes} byte(s), below "
                f"settings.workspace.disk.warn_free_bytes={disk.warn_free_bytes}."
            )
    elif estimate.remaining_free_bytes < DEFAULT_DISK_WARN_FREE_BYTES:
        builder.warnings.append(
            "Workspace source policy warning: cache filesystem has less than 2 GiB "
            f"free after estimated checkout near {cache_root.as_posix()}. "
            "Large workspaces or bundles may fail."
        )
    if cache_root.anchor != git_context.common_git_dir.anchor:
        builder.warnings.append(
            "Workspace source policy warning: cache root and Git common directory "
            "appear to be on different filesystem roots; workspace operations may "
            "copy more data."
        )


def disk_space_estimate(
    free_bytes: int,
    git_context: GitSourceContext,
    estimate_full_repository: bool,
) -> DiskSpaceEstimate:
    checkout_size_bytes = estimated_checkout_size_bytes(
        git_context,
        estimate_full_repository,
    )
    return DiskSpaceEstimate(
        checkout_size_bytes=checkout_size_bytes,
        remaining_free_bytes=max(0, free_bytes - checkout_size_bytes),
    )


def estimated_checkout_size_bytes(
    git_context: GitSourceContext,
    estimate_full_repository: bool = False,
) -> int:
    try:
        git_estimate = estimated_git_checkout_size_bytes(
            git_context,
            estimate_full_repository,
        )
    except subprocess.CalledProcessError:
        git_estimate = None
    if git_estimate is not None:
        return git_estimate
    return estimated_working_tree_size_bytes(git_context, estimate_full_repository)


def estimated_git_checkout_size_bytes(
    git_context: GitSourceContext,
    estimate_full_repository: bool = False,
) -> int | None:
    total = 0
    for record in git_zero_records(
        git_context.git_top_level,
        "ls-tree",
        "-l",
        "-r",
        "-z",
        git_context.source_tree,
    ):
        header, _, path = record.partition("\t")
        if not estimate_full_repository and not project_path_selected(
            path,
            git_context.project_root_relative_path,
        ):
            continue
        size_text = header.rsplit(" ", 1)[-1]
        if not size_text.isdigit():
            return None
        total += int(size_text)
    return total


def estimated_working_tree_size_bytes(
    git_context: GitSourceContext,
    estimate_full_repository: bool,
) -> int:
    scan_root = (
        git_context.git_top_level
        if estimate_full_repository
        else project_root_from_git_context(git_context)
    )
    project_root = project_root_from_git_context(git_context)
    reserved_roots: tuple[Path, ...] = (scan_root,)
    if project_root != scan_root and project_root.is_relative_to(scan_root):
        reserved_roots = (scan_root, project_root)
    if not scan_root.exists():
        return 0
    total = 0
    for current_root, dir_names, file_names in scan_root.walk():
        filter_estimate_dirs(current_root, dir_names, reserved_roots)
        for file_name in file_names:
            try:
                total += (current_root / file_name).lstat().st_size
            except OSError:
                continue
    return total


def filter_estimate_dirs(
    current_root: Path,
    dir_names: list[str],
    reserved_roots: tuple[Path, ...],
) -> None:
    retained: list[str] = []
    for dir_name in dir_names:
        candidate = current_root / dir_name
        if dir_name == ".git" or is_reserved_path(candidate, reserved_roots):
            continue
        retained.append(dir_name)
    dir_names[:] = retained


def is_reserved_path(path: Path, reserved_roots: tuple[Path, ...]) -> bool:
    for root in reserved_roots:
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if is_reserved_source_path(relative):
            return True
    return False


def project_root_from_git_context(git_context: GitSourceContext) -> Path:
    if git_context.project_root_relative_path == ".":
        return git_context.git_top_level
    return git_context.git_top_level / git_context.project_root_relative_path


def project_path_selected(path: str, project_root_relative_path: str) -> bool:
    if project_root_relative_path == ".":
        return True
    return path == project_root_relative_path or path.startswith(
        f"{project_root_relative_path}/"
    )
