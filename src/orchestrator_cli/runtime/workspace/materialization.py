from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import BoundedSemaphore

from orchestrator_cli.core.preflight.models import PreflightExecutionPlan


@dataclass(frozen=True)
class MaterializationLimiter:
    limit: int
    semaphore: BoundedSemaphore

    @classmethod
    def from_plan(cls, plan: PreflightExecutionPlan) -> MaterializationLimiter:
        limit = materialization_limit(plan)
        return cls(limit, BoundedSemaphore(limit))


@contextmanager
def workspace_materialization_slot(
    plan: PreflightExecutionPlan,
    limiter: MaterializationLimiter | None,
) -> Iterator[None]:
    resolved_limiter = limiter or MaterializationLimiter.from_plan(plan)
    resolved_limiter.semaphore.acquire()
    try:
        yield
    finally:
        resolved_limiter.semaphore.release()


def materialization_limit(plan: PreflightExecutionPlan) -> int:
    workspace = plan.runtime_config_snapshot.get("workspace")
    if not isinstance(workspace, dict):
        return 1
    value = workspace.get("max_concurrent_materializations")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return 1
