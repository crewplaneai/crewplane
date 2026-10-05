"""Apply terminal review decisions after their status artifacts are published."""

from __future__ import annotations

from crewplane.architecture.contracts import LogLevel

from ..common import (
    ExecutionTelemetry,
    RuntimeEventContext,
    emit_runtime_log,
    execution_console,
    should_print_console,
)
from ..errors import NodeExecutionError
from .types import ReviewLoopProgress, ReviewLoopRunContext


class ReviewPolicyError(NodeExecutionError):
    """A settled review policy decision that closes partial reuse."""


def _emit_consensus_exhaustion(
    telemetry: ExecutionTelemetry | None,
    node_id: str,
    executed_audit_rounds: int,
    continuation_reason: str | None,
) -> None:
    message = (
        f"Sequential node '{node_id}' failed to reach consensus after "
        f"{executed_audit_rounds} audit rounds."
    )
    if continuation_reason is not None:
        message = f"{message} Continuing due to {continuation_reason}."
    emit_runtime_log(
        telemetry,
        level=LogLevel.WARNING,
        message=message,
        operation="review_loop_consensus_exhausted",
        context=RuntimeEventContext(node_id=node_id),
        attributes={"continued": continuation_reason is not None},
    )


def _emit_no_canonical_candidate(
    telemetry: ExecutionTelemetry | None,
    node_id: str,
) -> None:
    emit_runtime_log(
        telemetry,
        level=LogLevel.ERROR,
        message=(
            f"Sequential node '{node_id}' did not produce any valid canonical "
            "candidate across all audit rounds."
        ),
        operation="review_loop_no_canonical_candidate",
        context=RuntimeEventContext(node_id=node_id),
    )


def apply_exhausted_review_loop_policy(
    context: ReviewLoopRunContext,
    progress: ReviewLoopProgress,
    continuation_reason: str | None,
) -> None:
    """Log the published decision before raising or printing its continuation.

    Missing candidates always fail. A continuation reason permits a valid candidate
    to continue. Logging and console errors take precedence over policy rejection.
    """
    if progress.latest_executor_outputs is None:
        _emit_no_canonical_candidate(context.telemetry, context.stage.id)
        raise ReviewPolicyError(
            f"Sequential node '{context.stage.id}' did not produce a valid canonical candidate."
        )

    _emit_consensus_exhaustion(
        telemetry=context.telemetry,
        node_id=context.stage.id,
        executed_audit_rounds=progress.executed_audit_rounds,
        continuation_reason=continuation_reason,
    )
    if continuation_reason is None:
        raise ReviewPolicyError(
            f"Sequential node '{context.stage.id}' failed to reach consensus after "
            f"{progress.executed_audit_rounds} audit rounds."
        )

    if should_print_console(context.telemetry):
        execution_console(context.telemetry).print(
            "[yellow]WARN[/] "
            f"Sequential node '{context.stage.id}' failed to reach consensus after "
            f"{progress.executed_audit_rounds} audit rounds. Continuing due to "
            f"{continuation_reason}."
        )


def apply_stalled_review_loop_policy(
    context: ReviewLoopRunContext,
    progress: ReviewLoopProgress,
) -> None:
    continued = progress.continued_after_stop
    message = (
        f"Sequential node '{context.stage.id}' stopped with no_progress after "
        f"{progress.stall.consecutive_round_count} consecutive unchanged "
        "remediation attempts. Review feedback remains unresolved."
    )
    if continued:
        message += " Continuing due to continue_on_failure=true."
    emit_runtime_log(
        context.telemetry,
        level=LogLevel.WARNING if continued else LogLevel.ERROR,
        message=message,
        operation="review_loop_stopped",
        context=RuntimeEventContext(node_id=context.stage.id),
        attributes={"stop_reason": "no_progress", "continued": continued},
    )
    if not continued:
        raise ReviewPolicyError(message)
