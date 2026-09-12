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
from crewplane.core.value_checks import is_nonnegative_int
from crewplane.core.workspace.checkout_size import (
    estimated_tree_checkout_size_bytes,
    estimated_working_tree_size_bytes,
)

from .git import git

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MaterializationCapacityRequest:
    """Describe the checkout whose disk capacity must be reserved."""

    target_path: Path
    source: WorkspaceSourceSnapshot
    estimate_full_repository: bool = False
    source_tree: str | None = None


@dataclass(frozen=True, slots=True)
class _DiskThresholds:
    fail_free_bytes: int | None
    warn_free_bytes: int | None


@dataclass(frozen=True, slots=True)
class _CapacityAdmission:
    probe_parent: Path
    estimated_bytes: int
    thresholds: _DiskThresholds


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

    def _reserve_capacity(self, admission: _CapacityAdmission) -> None:
        with self.admission_lock:
            free_bytes = _probe_free_bytes(admission)
            if free_bytes is not None:
                remaining = max(
                    0,
                    free_bytes
                    - self.admitted_estimated_bytes
                    - admission.estimated_bytes,
                )
                _enforce_disk_thresholds(remaining, admission.thresholds)
            self.admitted_estimated_bytes += admission.estimated_bytes

    def _release_capacity(self, estimated_bytes: int) -> None:
        if estimated_bytes == 0:
            return
        with self.admission_lock:
            self.admitted_estimated_bytes -= estimated_bytes


@contextmanager
def workspace_materialization_slot(
    plan: PreflightExecutionPlan,
    limiter: MaterializationLimiter | None,
    capacity_request: MaterializationCapacityRequest | None = None,
) -> Iterator[None]:
    resolved_limiter = limiter or MaterializationLimiter.from_plan(plan)
    with resolved_limiter.semaphore:
        admitted_bytes = 0
        try:
            if capacity_request is not None:
                admitted_bytes = _admit_materialization_capacity(
                    plan,
                    resolved_limiter,
                    capacity_request,
                )
            yield
        finally:
            resolved_limiter._release_capacity(admitted_bytes)


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
    request: MaterializationCapacityRequest,
) -> int:
    admission = _capacity_admission(plan, request)
    limiter._reserve_capacity(admission)
    return admission.estimated_bytes


def _capacity_admission(
    plan: PreflightExecutionPlan,
    request: MaterializationCapacityRequest,
) -> _CapacityAdmission:
    estimated_bytes = estimated_checkout_size(
        request.source,
        request.estimate_full_repository,
        request.source_tree,
    )
    return _CapacityAdmission(
        probe_parent=_existing_parent(request.target_path),
        estimated_bytes=estimated_bytes,
        thresholds=_disk_thresholds(plan),
    )


def _probe_free_bytes(admission: _CapacityAdmission) -> int | None:
    try:
        return shutil.disk_usage(admission.probe_parent).free
    except OSError as exc:
        if admission.thresholds.fail_free_bytes is not None:
            raise RuntimeError(
                "Workspace materialization capacity probe failed while a "
                "failure threshold is configured."
            ) from exc
        LOGGER.warning(
            "Workspace materialization capacity probe failed; continuing "
            "without a configured failure threshold: %s",
            exc,
        )
        return None


def _enforce_disk_thresholds(
    remaining_bytes: int,
    thresholds: _DiskThresholds,
) -> None:
    if (
        thresholds.fail_free_bytes is not None
        and remaining_bytes < thresholds.fail_free_bytes
    ):
        raise RuntimeError(
            "Workspace materialization capacity is below "
            "settings.workspace.disk.fail_free_bytes."
        )
    if (
        thresholds.warn_free_bytes is not None
        and remaining_bytes < thresholds.warn_free_bytes
    ):
        LOGGER.warning(
            "Workspace materialization capacity is below the configured "
            "warning threshold after current admissions."
        )


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
) -> _DiskThresholds:
    workspace = plan.runtime_config_snapshot.get("workspace")
    disk = workspace.get("disk") if isinstance(workspace, dict) else None
    if not isinstance(disk, dict):
        return _DiskThresholds(None, None)
    fail = disk.get("fail_free_bytes")
    warn = disk.get("warn_free_bytes")
    return _DiskThresholds(
        _optional_nonnegative_int(fail),
        _optional_nonnegative_int(warn),
    )


def _optional_nonnegative_int(value: object) -> int | None:
    if is_nonnegative_int(value):
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
