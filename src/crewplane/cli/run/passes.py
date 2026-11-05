from __future__ import annotations

from pathlib import Path
from typing import Protocol

from rich.console import Console

from crewplane.cli.memory.extraction import ExtractionCleanupError, extract_memory
from crewplane.cli.memory.rendering import print_literal
from crewplane.cli.memory.review import interactive, review_candidates
from crewplane.cli.workflow_context import (
    CliWorkflowContext,
    load_cli_workflow_context,
    raise_context_loading_failure,
)
from crewplane.core.config import Config
from crewplane.core.preflight.foreach.compilation import inventory_workflow
from crewplane.core.preflight.source import PreflightWorkflowSource
from crewplane.runtime.agent.process.drain import unconfirmed_process_cleanup

from .completion import CompletedRun
from .observability import WorkflowCancelledByUser


class WorkflowPass(Protocol):
    async def __call__(
        self,
        config: Config,
        source: PreflightWorkflowSource,
        project_root: Path,
        state_dir: Path,
        force: bool,
        no_live: bool,
        console: Console,
    ) -> CompletedRun: ...


async def execute_passes(
    context: CliWorkflowContext,
    force: bool,
    no_live: bool,
    console: Console,
    execute_pass: WorkflowPass,
) -> None:
    paths = context.paths
    repeat_count = context.source.workflow.repeat_force_run_count
    effective_force = force or repeat_count is not None
    candidates: set[str] = set()
    candidate_run_ids: set[str] = set()
    original_error: Exception | None = None
    optional_work_safe = True
    for pass_index in range(repeat_count or 1):
        if repeat_count is not None:
            console.print(f"Run {pass_index + 1} of {repeat_count} (fresh)")
        if pass_index:
            try:
                context = load_cli_workflow_context(paths, require_project_root=True)
            except Exception as exc:
                raise_context_loading_failure(
                    exc, console, paths=paths, write_diagnostics=True
                )
        completion = await execute_pass(
            context.config,
            context.source,
            project_root=paths.project_root,
            state_dir=paths.state_dir,
            force=effective_force,
            no_live=no_live,
            console=console,
        )
        if completion.kind != "dedupe" and not completion.can_learn:
            optional_work_safe = False
        if (
            optional_work_safe
            and completion.can_learn
            and context.config.settings.memory.learn
            and any(node.memory for node in inventory_workflow(context.source).nodes)
        ):
            try:
                result = await extract_memory(context.config, completion, console)
                candidates.update(identity.record_id for identity in result.candidates)
                if result.candidates and completion.plan is not None:
                    candidate_run_ids.add(completion.plan.run_id)
                print_literal(console, result.message)
            except WorkflowCancelledByUser:
                raise
            except Exception as exc:
                if unconfirmed_process_cleanup(exc) is not None or isinstance(
                    exc, ExtractionCleanupError
                ):
                    raise
                print_literal(
                    console,
                    f"Project memory learning failed separately; parent outcome is unchanged: {exc}",
                )
        if completion.terminal_error is not None:
            original_error = completion.terminal_error
            break
    if candidates and optional_work_safe:
        if interactive(console):
            try:
                await review_candidates(
                    paths.project_root, console, record_ids=frozenset(candidates)
                )
            except (OSError, ValueError) as exc:
                print_literal(
                    console,
                    f"Memory review stopped after error; inspect current store state before retrying: {exc}",
                )
        else:
            for run_id in sorted(candidate_run_ids):
                print_literal(
                    console,
                    f"Memory candidates remain pending. Review: crewplane memory review --run {run_id}",
                )
    if original_error is not None:
        raise original_error
