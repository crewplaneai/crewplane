from __future__ import annotations

import asyncio
import io
import json
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from threading import Event, get_ident
from unittest.mock import AsyncMock, Mock

import pytest
import typer
from rich.console import Console

from crewplane.cli import workflow_runner
from crewplane.cli.memory.extraction import ExtractionResult
from crewplane.cli.run import passes
from crewplane.cli.run.completion import CompletedRun
from crewplane.cli.workflow_context import (
    CliWorkflowContext,
    CliWorkflowPaths,
    load_cli_workflow_context,
)
from crewplane.core.memory.lifecycle import CandidateIdentity
from crewplane.runtime.agent.process.drain import (
    ProcessDrainError,
    ProcessDrainEvidence,
)
from scripts.validation_policy import load_policy
from tests.helpers.resume import make_plan
from tests.integration.cli.repeat_force_run_support import create_project

TIMEOUTS = load_policy().timeouts


@pytest.fixture
def pass_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CliWorkflowContext:
    monkeypatch.chdir(tmp_path)
    project = create_project(tmp_path, 3, node_count=1)
    project.workflow["nodes"][0]["memory"] = True
    project.write()
    return load_cli_workflow_context(
        CliWorkflowPaths(
            project.workflow_path,
            project.config_path,
            tmp_path,
            tmp_path / ".crewplane",
        )
    )


def learnable_completion(context: CliWorkflowContext, run_id: str) -> CompletedRun:
    return CompletedRun(
        kind="executed",
        project_root=context.paths.project_root,
        state_dir=context.paths.state_dir,
        plan=make_plan().model_copy(update={"run_id": run_id}),
        outcome="succeeded",
        manifest_committed=True,
        cleanup_confirmed=True,
        lock_released=True,
        result_displayed=True,
    )


def candidate_result(record_id: str) -> ExtractionResult:
    return ExtractionResult(
        (CandidateIdentity(record_id, "revision", "digest", None),),
        f"Retained {record_id}",
    )


@pytest.mark.parametrize("review_error_type", [OSError, ValueError, RuntimeError])
def test_review_error_boundary_preserves_deferred_parent_failure(
    pass_context: CliWorkflowContext,
    monkeypatch: pytest.MonkeyPatch,
    review_error_type: type[Exception],
) -> None:
    output = io.StringIO()
    console = Console(file=output, width=200)
    parent_error = RuntimeError("parent failed")
    review_error = review_error_type("review failed")
    first = learnable_completion(pass_context, "first")
    last = replace(first, outcome="failed", terminal_error=parent_error)
    execute = AsyncMock(side_effect=[first, last])
    extract = AsyncMock(
        side_effect=[candidate_result("lesson"), ValueError("extraction failed")]
    )
    review = AsyncMock(side_effect=review_error)
    monkeypatch.setattr(
        passes, "load_cli_workflow_context", Mock(return_value=pass_context)
    )
    monkeypatch.setattr(passes, "extract_memory", extract)
    monkeypatch.setattr(passes, "interactive", Mock(return_value=True))
    monkeypatch.setattr(passes, "review_candidates", review)
    expected = review_error if review_error_type is RuntimeError else parent_error

    with pytest.raises(type(expected)) as captured:
        asyncio.run(passes.execute_passes(pass_context, False, True, console, execute))

    assert captured.value is expected
    assert execute.await_count == extract.await_count == 2
    review.assert_awaited_once_with(
        pass_context.paths.project_root, console, record_ids=frozenset({"lesson"})
    )
    assert "parent outcome is unchanged: extraction failed" in output.getvalue()
    assert ("Memory review stopped after error" in output.getvalue()) == (
        review_error_type is not RuntimeError
    )
    assert "Run 3 of" not in output.getvalue()


def test_noninteractive_review_commands_use_sorted_unique_run_ids(
    pass_context: CliWorkflowContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = io.StringIO()
    execute = AsyncMock(
        side_effect=[
            learnable_completion(pass_context, run_id) for run_id in ("z", "a", "z")
        ]
    )
    monkeypatch.setattr(
        passes, "load_cli_workflow_context", Mock(return_value=pass_context)
    )
    monkeypatch.setattr(
        passes, "extract_memory", AsyncMock(return_value=candidate_result("lesson"))
    )
    monkeypatch.setattr(passes, "interactive", Mock(return_value=False))
    review = AsyncMock()
    monkeypatch.setattr(passes, "review_candidates", review)

    result = asyncio.run(
        passes.execute_passes(
            pass_context, False, True, Console(file=output, width=200), execute
        )
    )

    assert result is None
    assert output.getvalue().splitlines()[-2:] == [
        f"Memory candidates remain pending. Review: crewplane memory review --run {run_id}"
        for run_id in ("a", "z")
    ]
    review.assert_not_awaited()


@pytest.mark.parametrize("failure", ["process_cleanup", "task_cancellation"])
def test_fatal_extraction_discards_queued_review_and_preserves_error_identity(
    pass_context: CliWorkflowContext, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    error: BaseException
    if failure == "process_cleanup":
        error = ValueError("extraction failed during cleanup")
        error.__cause__ = ProcessDrainError(
            ProcessDrainEvidence(123, 123, True, False), "process group still alive"
        )
    else:
        error = asyncio.CancelledError("extraction cancelled")
    execute = AsyncMock(return_value=learnable_completion(pass_context, "first"))
    extract = AsyncMock(side_effect=[candidate_result("lesson"), error])
    review = AsyncMock()
    monkeypatch.setattr(
        passes, "load_cli_workflow_context", Mock(return_value=pass_context)
    )
    monkeypatch.setattr(passes, "extract_memory", extract)
    monkeypatch.setattr(passes, "interactive", Mock(return_value=True))
    monkeypatch.setattr(passes, "review_candidates", review)

    with pytest.raises(type(error)) as captured:
        asyncio.run(
            passes.execute_passes(
                pass_context, False, True, Console(file=io.StringIO()), execute
            )
        )

    assert captured.value is error
    assert execute.await_count == extract.await_count == 2
    review.assert_not_awaited()


@pytest.mark.race
@pytest.mark.parametrize("phase", ["reload_success", "reload_failure", "diagnostics"])
@pytest.mark.parametrize("cancel_count", [0, 2])
def test_reload_drains_io_and_diagnostics_before_cancellation_or_failure(
    pass_context: CliWorkflowContext,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    cancel_count: int,
) -> None:
    pass_context.source.workflow.repeat_force_run_count = 2
    release = Event()
    finished = Event()
    reload_error = ValueError("reload invalid")
    console_output = io.StringIO()
    execute = AsyncMock(
        return_value=CompletedRun(
            "dedupe", pass_context.paths.project_root, pass_context.paths.state_dir
        )
    )
    original_load = passes.load_cli_workflow_context
    original_diagnostics = workflow_runner.write_early_preflight_failure_run
    context_value = ContextVar("pass_reload_context", default="missing")

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        loop_thread = get_ident()
        entered = asyncio.Event()
        context_value.set("preserved")

        def block_worker() -> None:
            loop.call_soon_threadsafe(entered.set)
            try:
                assert release.wait(TIMEOUTS.scenario_seconds), "worker not released"
            finally:
                finished.set()

        def reload_context(
            paths: CliWorkflowPaths, require_project_root: bool = False
        ) -> CliWorkflowContext:
            assert get_ident() != loop_thread, "reload blocked the event loop"
            assert context_value.get() == "preserved"
            assert require_project_root
            if phase != "diagnostics":
                block_worker()
            if phase != "reload_success":
                raise reload_error
            return original_load(paths, require_project_root)

        def write_diagnostics(*args, **kwargs) -> None:
            assert get_ident() != loop_thread, "diagnostics blocked the event loop"
            if phase == "diagnostics":
                block_worker()
            original_diagnostics(*args, **kwargs)

        monkeypatch.setattr(passes, "load_cli_workflow_context", reload_context)
        monkeypatch.setattr(
            workflow_runner, "write_early_preflight_failure_run", write_diagnostics
        )
        task = asyncio.create_task(
            passes.execute_passes(
                pass_context,
                False,
                True,
                Console(file=console_output, width=200),
                execute,
            )
        )
        readiness = asyncio.create_task(entered.wait())
        try:
            async with asyncio.timeout(TIMEOUTS.scenario_seconds):
                await asyncio.wait_for(
                    asyncio.wait(
                        {task, readiness}, return_when=asyncio.FIRST_COMPLETED
                    ),
                    TIMEOUTS.readiness_seconds,
                )
                assert entered.is_set(), "reload did not reach its worker"
                for index in range(cancel_count):
                    task.cancel(f"cancel reload {index}")
                    await asyncio.sleep(0)
                    assert task.cancelling() == index + 1
                    assert not task.done()
                assert execute.await_count == 1
                assert not finished.is_set()
                release.set()
                if cancel_count:
                    with pytest.raises(asyncio.CancelledError) as captured:
                        await task
                    assert str(captured.value) == "cancel reload 0"
                    if phase != "reload_success":
                        exit_error = captured.value.__cause__
                        assert isinstance(exit_error, typer.Exit)
                        assert exit_error.__cause__ is reload_error
                elif phase != "reload_success":
                    with pytest.raises(typer.Exit) as failure:
                        await task
                    assert failure.value.exit_code == 1
                    assert failure.value.__cause__ is reload_error
                else:
                    assert await task is None
                assert finished.is_set()
                assert execute.await_count == (
                    2 if not cancel_count and phase == "reload_success" else 1
                )
                if phase != "reload_success":
                    assert "Run 2 of 2 (fresh)" in console_output.getvalue()
                    assert "Invalid: reload invalid" in console_output.getvalue()
                    (diagnostics,) = pass_context.paths.state_dir.glob(
                        "execution-stages/*/preflight/diagnostics.json"
                    )
                    assert "reload invalid" in json.dumps(
                        json.loads(diagnostics.read_bytes())
                    )
        finally:
            release.set()
            task.cancel()
            readiness.cancel()
            async with asyncio.timeout(TIMEOUTS.cleanup_seconds):
                await asyncio.gather(task, readiness, return_exceptions=True)

    asyncio.run(scenario())
