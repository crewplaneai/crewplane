from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import BoundedSemaphore, Lock

from crewplane.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSourceSnapshot,
)
from crewplane.core.workspace.checkout_size import (
    estimated_tree_checkout_size_bytes,
    estimated_working_tree_size_bytes,
)

from .git import git

LOGGER = logging.getLogger(__name__)


@dataclass
class MaterializationLimiter:
    limit: int
    semaphore: BoundedSemaphore
    admission_lock: Lock
    admitted_estimated_bytes: int = 0

    @classmethod
    def from_plan(cls, plan: PreflightExecutionPlan) -> MaterializationLimiter:
        limit = materialization_limit(plan)
        return cls(limit, BoundedSemaphore(limit), Lock())


@contextmanager
def workspace_materialization_slot(
    plan: PreflightExecutionPlan,
    limiter: MaterializationLimiter | None,
    target_path: Path | None = None,
    source: WorkspaceSourceSnapshot | None = None,
    estimate_full_repository: bool = False,
    source_tree: str | None = None,
) -> Iterator[None]:
    resolved_limiter = limiter or MaterializationLimiter.from_plan(plan)
    resolved_limiter.semaphore.acquire()
    admitted_bytes = 0
    try:
        if target_path is not None and source is not None:
            admitted_bytes = _admit_materialization_capacity(
                plan,
                resolved_limiter,
                target_path,
                source,
                estimate_full_repository,
                source_tree,
            )
        yield
    finally:
        if admitted_bytes:
            with resolved_limiter.admission_lock:
                resolved_limiter.admitted_estimated_bytes -= admitted_bytes
        resolved_limiter.semaphore.release()


def materialization_limit(plan: PreflightExecutionPlan) -> int:
    workspace = plan.runtime_config_snapshot.get("workspace")
    if not isinstance(workspace, dict):
        return 1
    value = workspace.get("max_concurrent_materializations")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return 1


def _admit_materialization_capacity(
    plan: PreflightExecutionPlan,
    limiter: MaterializationLimiter,
    target_path: Path,
    source: WorkspaceSourceSnapshot,
    estimate_full_repository: bool,
    source_tree: str | None,
) -> int:
    estimated_bytes = estimated_checkout_size(
        source,
        estimate_full_repository,
        source_tree,
    )
    fail_free_bytes, warn_free_bytes = _disk_thresholds(plan)
    probe_parent = _existing_parent(target_path)
    with limiter.admission_lock:
        try:
            free_bytes = shutil.disk_usage(probe_parent).free
        except OSError as exc:
            if fail_free_bytes is not None:
                raise RuntimeError(
                    "Workspace materialization capacity probe failed while a "
                    "failure threshold is configured."
                ) from exc
            LOGGER.warning(
                "Workspace materialization capacity probe failed; continuing "
                "without a configured failure threshold: %s",
                exc,
            )
            limiter.admitted_estimated_bytes += estimated_bytes
            return estimated_bytes
        remaining = max(
            0,
            free_bytes - limiter.admitted_estimated_bytes - estimated_bytes,
        )
        if fail_free_bytes is not None and remaining < fail_free_bytes:
            raise RuntimeError(
                "Workspace materialization capacity is below "
                "settings.workspace.disk.fail_free_bytes."
            )
        if warn_free_bytes is not None and remaining < warn_free_bytes:
            LOGGER.warning(
                "Workspace materialization capacity is below the configured "
                "warning threshold after current admissions."
            )
        limiter.admitted_estimated_bytes += estimated_bytes
    return estimated_bytes


def estimated_checkout_size(
    source: WorkspaceSourceSnapshot,
    estimate_full_repository: bool = False,
    source_tree: str | None = None,
) -> int:
    try:
        records = git(Path(source.git_top_level)).zero_records(
            "ls-tree",
            "-l",
            "-r",
            "-z",
            source_tree or source.source_tree,
        )
    except subprocess.CalledProcessError:
        return _estimated_working_tree_size(source, estimate_full_repository)
    estimate = estimated_tree_checkout_size_bytes(
        records,
        source.project_root_relative_path,
        estimate_full_repository,
    )
    if estimate is None:
        return _estimated_working_tree_size(source, estimate_full_repository)
    return estimate


def _estimated_working_tree_size(
    source: WorkspaceSourceSnapshot,
    estimate_full_repository: bool,
) -> int:
    root = Path(source.git_top_level)
    if not estimate_full_repository and source.project_root_relative_path != ".":
        root /= source.project_root_relative_path
    project_root = Path(source.git_top_level) / source.project_root_relative_path
    reserved_roots = (root, project_root) if project_root != root else (root,)
    return estimated_working_tree_size_bytes(root, reserved_roots)


def _disk_thresholds(
    plan: PreflightExecutionPlan,
) -> tuple[int | None, int | None]:
    workspace = plan.runtime_config_snapshot.get("workspace")
    disk = workspace.get("disk") if isinstance(workspace, dict) else None
    if not isinstance(disk, dict):
        return None, None
    fail = disk.get("fail_free_bytes")
    warn = disk.get("warn_free_bytes")
    return (_optional_nonnegative_int(fail), _optional_nonnegative_int(warn))


def _optional_nonnegative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _existing_parent(target_path: Path) -> Path:
    current = target_path
    while not current.exists():
        parent = current.parent
        if parent == current:
            raise RuntimeError(
                f"Workspace capacity probe has no existing parent: {target_path}."
            )
        current = parent
    return current
