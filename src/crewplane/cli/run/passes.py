from __future__ import annotations

import asyncio
from contextvars import copy_context
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from rich.console import Console

from crewplane.cli.memory.extraction import ExtractionCleanupError, extract_memory
from crewplane.cli.memory.rendering import print_literal
from crewplane.cli.memory.review import interactive, review_candidates
from crewplane.cli.workflow_context import (
    CliWorkflowContext,
    CliWorkflowPaths,
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
    """Execute one pass through the CLI-owned run lifecycle."""

    async def __call__(
        self,
        config: Config,
        source: PreflightWorkflowSource,
        project_root: Path,
        state_dir: Path,
        force: bool,
        no_live: bool,
        console: Console,
    ) -> CompletedRun:
        """Return the pass outcome and its lifecycle guarantees.

        Finish owned finalization, cleanup, lock-release, and display work
        before returning; report the guarantees achieved in CompletedRun.
        Return deferred parent failures in terminal_error so eligible learning
        can finish. Let cancellation and non-deferred errors propagate.
        """
        ...


async def execute_passes(
    context: CliWorkflowContext,
    force: bool,
    no_live: bool,
    console: Console,
    execute_pass: WorkflowPass,
) -> None:
    """Execute serial passes, then review collected memory candidates once.

    Fix the repetition count from the initial context (one pass if absent),
    forcing fresh runs when it is set. Use the initial context for the first
    pass and reload config and workflow inputs before every later pass, after
    printing its header. Reloads still require the project-root workspace.

    Extract only after eligible completions. An unsafe non-dedupe completion
    disables learning and review for the remaining sequence. Review candidates
    after the sequence, then raise any deferred parent failure; noninteractive
    runs print review commands in sorted run-ID order instead.

    Report ordinary extraction errors and review OSError/ValueError failures
    without changing the parent outcome. Other review errors propagate.
    Fatal callback, extraction cleanup, and cancellation errors stop the
    sequence without review. Cancellation during a reload waits for its I/O
    and failure diagnostics, then propagates with any worker error as its
    cause. Other reload failures retain their diagnostic CLI exit and cause.
    """
    paths = context.paths
    repeat_count = context.source.workflow.repeat_force_run_count
    effective_force = force or repeat_count is not None
    memory = _PassMemoryCandidates(console)
    original_error: Exception | None = None
    for pass_index in range(repeat_count or 1):
        if repeat_count is not None:
            console.print(f"Run {pass_index + 1} of {repeat_count} (fresh)")
        if pass_index:
            context = await _reload_pass_context(paths, console)
        completion = await execute_pass(
            context.config,
            context.source,
            project_root=paths.project_root,
            state_dir=paths.state_dir,
            force=effective_force,
            no_live=no_live,
            console=console,
        )
        await memory.collect(context, completion)
        if completion.terminal_error is not None:
            original_error = completion.terminal_error
            break
    await memory.review(paths.project_root)
    if original_error is not None:
        raise original_error


async def _reload_pass_context(
    paths: CliWorkflowPaths, console: Console
) -> CliWorkflowContext:
    """Own the reload and its diagnostics until I/O settles, even on cancellation."""
    worker = asyncio.get_running_loop().run_in_executor(
        None, copy_context().run, _load_pass_context, paths, console
    )
    cancellation: asyncio.CancelledError | None = None
    while not worker.done():
        try:
            await asyncio.wait((worker,))
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
    try:
        context = worker.result()
    except BaseException as exc:
        if cancellation is not None:
            raise cancellation from exc
        raise
    if cancellation is not None:
        raise cancellation
    return context


def _load_pass_context(paths: CliWorkflowPaths, console: Console) -> CliWorkflowContext:
    try:
        return load_cli_workflow_context(paths, require_project_root=True)
    except Exception as exc:
        raise_context_loading_failure(exc, console, paths=paths, write_diagnostics=True)


@dataclass
class _PassMemoryCandidates:
    console: Console
    record_ids: set[str] = field(default_factory=set)
    run_ids: set[str] = field(default_factory=set)
    optional_work_safe: bool = True

    async def collect(
        self, context: CliWorkflowContext, completion: CompletedRun
    ) -> None:
        if completion.kind != "dedupe" and not completion.can_learn:
            self.optional_work_safe = False
        if not (
            self.optional_work_safe
            and completion.can_learn
            and context.config.settings.memory.learn
            and any(node.memory for node in inventory_workflow(context.source).nodes)
        ):
            return
        try:
            result = await extract_memory(context.config, completion, self.console)
            self.record_ids.update(identity.record_id for identity in result.candidates)
            if result.candidates and completion.plan is not None:
                self.run_ids.add(completion.plan.run_id)
            print_literal(self.console, result.message)
        except WorkflowCancelledByUser:
            raise
        except Exception as exc:
            if unconfirmed_process_cleanup(exc) is not None or isinstance(
                exc, ExtractionCleanupError
            ):
                raise
            print_literal(
                self.console,
                f"Project memory learning failed separately; parent outcome is unchanged: {exc}",
            )

    async def review(self, project_root: Path) -> None:
        if not self.record_ids or not self.optional_work_safe:
            return
        if not interactive(self.console):
            for run_id in sorted(self.run_ids):
                print_literal(
                    self.console,
                    f"Memory candidates remain pending. Review: crewplane memory review --run {run_id}",
                )
            return
        try:
            await review_candidates(
                project_root, self.console, record_ids=frozenset(self.record_ids)
            )
        except (OSError, ValueError) as exc:
            print_literal(
                self.console,
                f"Memory review stopped after error; inspect current store state before retrying: {exc}",
            )
