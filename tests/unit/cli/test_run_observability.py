from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import NoReturn, cast

import pytest

from crewplane.cli.run import observability as observability_module
from crewplane.core.workflow.models import WorkflowPlan
from crewplane.observability import PersistentRunLogger, RunResult


@dataclass
class RecordingSummaryLogger:
    refreshed_results: list[RunResult] = field(default_factory=list)
    failure_events: list[tuple[str, str, str]] = field(default_factory=list)
    last_summary: None = None

    def refresh_summary(self, result: RunResult) -> None:
        self.refreshed_results.append(result)

    def record_failure_summary_event(
        self,
        workflow_name: str,
        run_id: str,
        message: str,
    ) -> None:
        self.failure_events.append((workflow_name, run_id, message))


def reject_bounded_refresh(
    target: Callable[[], None],
    name: str,
    timeout_seconds: float,
) -> NoReturn:
    del target, name, timeout_seconds
    raise AssertionError("final summary refresh must complete before returning")


def test_successful_summary_refresh_is_not_abandoned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = RecordingSummaryLogger()
    monkeypatch.setattr(
        observability_module,
        "run_best_effort_thread",
        reject_bounded_refresh,
    )

    result = observability_module.refresh_successful_run_summary(
        cast(PersistentRunLogger, logger)
    )

    assert result is logger
    assert logger.refreshed_results == [RunResult(status="succeeded")]


def test_cancelled_summary_refresh_is_not_abandoned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = RecordingSummaryLogger()
    monkeypatch.setattr(
        observability_module,
        "run_best_effort_thread",
        reject_bounded_refresh,
    )

    result = observability_module.refresh_cancelled_run_summary(
        cast(PersistentRunLogger, logger),
        "external_cancellation",
    )

    assert result is logger
    assert logger.refreshed_results == [
        RunResult(status="cancelled", cancel_reason="external_cancellation")
    ]


def test_failed_summary_refresh_is_not_abandoned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = RecordingSummaryLogger()
    workflow = WorkflowPlan(name="workflow", nodes=[])
    error = RuntimeError("provider failed")
    monkeypatch.setattr(
        observability_module,
        "run_best_effort_thread",
        reject_bounded_refresh,
    )

    result = observability_module.refresh_failed_run_summary(
        cast(PersistentRunLogger, logger),
        workflow,
        "run-1",
        error,
    )

    assert result is logger
    assert logger.failure_events == [("workflow", "run-1", "provider failed")]
    assert logger.refreshed_results == [RunResult(status="failed")]
