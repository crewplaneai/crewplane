from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from rich.console import Console

from crewplane.artifacts.memory.completion import RunCompletionReceipt, save_completion
from crewplane.cli.memory.rendering import print_literal
from crewplane.core.execution_state import TerminalRunStatus
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.observability import PersistentRunLogger

from .branch_export_output import print_branch_export_fulfillments
from .observability import print_end_of_run_summary


@dataclass
class CompletionDisplay:
    logger: PersistentRunLogger | None = None
    branch_exports: tuple[Path, ...] = ()

    def display(self, console: Console) -> bool:
        print_branch_export_fulfillments(self.branch_exports, console)
        print_end_of_run_summary(console, self.logger)
        return self.logger is not None and self.logger.last_summary is not None


@dataclass(frozen=True)
class CompletedRun:
    kind: Literal["executed", "dedupe", "ineligible"]
    project_root: Path
    state_dir: Path
    plan: PreflightExecutionPlan | None = None
    stages_dir: Path | None = None
    results_dir: Path | None = None
    outcome: TerminalRunStatus | None = None
    terminal_error: Exception | None = None
    manifest_committed: bool = False
    cleanup_confirmed: bool = False
    lock_released: bool = False
    result_displayed: bool = False

    @property
    def can_learn(self) -> bool:
        return (
            self.kind == "executed"
            and self.outcome in {"succeeded", "failed"}
            and all(
                (
                    self.manifest_committed,
                    self.cleanup_confirmed,
                    self.lock_released,
                    self.result_displayed,
                )
            )
        )


@dataclass
class RunArtifactLocations:
    """CLI-owned location capture retained even when an extraction is cancelled."""

    plan: PreflightExecutionPlan | None = None
    stages_dir: Path | None = None
    results_dir: Path | None = None
    cleanup_confirmed: bool | None = None
    lock_released: bool | None = None


def persist_memory_completion(
    completion: CompletedRun, console: Console
) -> CompletedRun:
    if (
        completion.kind != "executed"
        or completion.plan is None
        or completion.stages_dir is None
        or completion.outcome not in {"succeeded", "failed"}
    ):
        return completion
    receipt = RunCompletionReceipt(
        run_id=completion.plan.run_id,
        run_key_name=completion.plan.run_key_name,
        workflow_signature=completion.plan.workflow_signature,
        outcome=completion.outcome,
        manifest_committed=completion.manifest_committed,
        cleanup_confirmed=completion.cleanup_confirmed,
        lock_released=completion.lock_released,
        result_displayed=completion.result_displayed,
    )
    try:
        save_completion(completion.stages_dir, receipt)
    except (OSError, ValueError) as exc:
        print_literal(
            console,
            f"Could not retain memory learning completion proof; optional learning skipped and parent outcome unchanged: {exc}",
        )
        return replace(completion, kind="ineligible")
    return completion
