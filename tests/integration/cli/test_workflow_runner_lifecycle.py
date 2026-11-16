from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest
import typer
from rich.console import Console

from crewplane.architecture.errors import AdapterContractError
from crewplane.artifacts.locks import SameContextLock
from crewplane.artifacts.manager import OutputManager
from crewplane.artifacts.resume.decision import ResumeDecision
from crewplane.artifacts.resume.validation import ValidatedResumeFrontier
from crewplane.artifacts.run_history import RunHistoryRecord
from crewplane.cli.run import execution, execution_helpers
from crewplane.cli.run.observability import WorkflowCancelledByUser
from crewplane.cli.run.resume import ResumePlan
from crewplane.core.preflight import PreflightExecutionPlan
from tests.helpers.resume import make_run_manifest
from tests.integration.cli.workflow_runner_support import (
    mock_runner_config,
    run_directories,
    run_workflow,
    runner_workflow,
)


@pytest.mark.parametrize("phase", ["snapshot", "compilation"])
@pytest.mark.parametrize("error_type", [RuntimeError, AdapterContractError])
def test_preflight_preserves_exception_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    error_type: type[Exception],
) -> None:
    monkeypatch.chdir(tmp_path)
    failure = error_type("preflight failed")
    operation = (
        "build_runtime_config_snapshot" if phase == "snapshot" else "compile_preview"
    )
    monkeypatch.setattr(execution, operation, Mock(side_effect=failure))
    converted = phase == "snapshot" or error_type is AdapterContractError

    with pytest.raises(typer.Exit if converted else error_type) as raised:
        asyncio.run(
            run_workflow(
                runner_workflow(),
                mock_runner_config(),
                Console(file=io.StringIO(), force_terminal=False),
            )
        )

    assert (raised.value.__cause__ if converted else raised.value) is failure
    assert not list((tmp_path / ".crewplane/locks").glob("*"))
    directories = run_directories(tmp_path)
    assert len(directories) == int(converted)
    if converted:
        diagnostics = json.loads(
            (directories[0] / "preflight/diagnostics.json").read_text(encoding="utf-8")
        )
        assert diagnostics[0]["code"] == "RUNTIME-CONFIG"
        assert diagnostics[0]["message"] == "preflight failed"


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("execution failed"),
        WorkflowCancelledByUser("dashboard stopped"),
        asyncio.CancelledError("task cancelled"),
    ],
)
def test_execution_failure_prints_one_summary_and_reraises_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    monkeypatch.chdir(tmp_path)
    stream = io.StringIO()
    exports = Mock()
    monkeypatch.setattr(execution, "fulfill_branch_exports", exports)

    async def fail_execution(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise failure

    with pytest.raises(type(failure)) as raised:
        asyncio.run(
            run_workflow(
                runner_workflow(),
                mock_runner_config(),
                Console(file=stream, force_terminal=False),
                execute_workflow_impl=fail_execution,
            )
        )

    assert raised.value is failure
    assert stream.getvalue().count("Run Summary:") == 1
    exports.assert_not_called()
    assert not list((tmp_path / ".crewplane/locks").iterdir())


@pytest.mark.parametrize(
    "operation", ["write_preflight_static_file", "write_preflight_plan"]
)
def test_materialization_failure_preserves_error_and_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "input.txt").write_bytes(b"static input\n")
    failure = OSError("artifact publication failed")
    monkeypatch.setattr(OutputManager, operation, Mock(side_effect=failure))
    components, scheduler = Mock(), Mock()
    monkeypatch.setattr(execution, "build_components_for_run", components)

    with pytest.raises(OSError, match="artifact publication failed") as raised:
        asyncio.run(
            run_workflow(
                runner_workflow("{{file:input.txt}}"),
                mock_runner_config(),
                Console(file=io.StringIO(), force_terminal=False),
                execute_workflow_impl=scheduler,
            )
        )

    assert raised.value is failure
    components.assert_not_called()
    scheduler.assert_not_called()
    (run_dir,) = run_directories(tmp_path)
    assert not (run_dir / "preflight/execution-plan.json").exists()
    assert not (run_dir / "manifests/run.json").exists()
    assert not list((tmp_path / ".crewplane/locks").iterdir())


@pytest.mark.race
@pytest.mark.parametrize("phase", ["lock", "history", "exports"])
@pytest.mark.parametrize("worker_fails", [False, True])
def test_blocking_run_work_settles_before_cancellation_releases_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, worker_fails: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    entered = Event()
    release = Event()
    finished = Event()
    failure = RuntimeError("blocking operation failed")
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=False)
    config = mock_runner_config()
    workflow = runner_workflow()

    if phase == "history":
        asyncio.run(run_workflow(workflow, config, console))
        stream.truncate(0)
        stream.seek(0)
        module, name = execution_helpers, "fulfill_branch_exports_from_history"
    elif phase == "exports":
        module, name = execution, "fulfill_branch_exports"
    else:
        module, name = execution, "acquire_same_context_lock"
    operation = getattr(module, name)

    def block_operation(*args: object, **kwargs: object) -> object:
        result = (
            operation(*args, **kwargs) if phase == "lock" and not worker_fails else None
        )
        entered.set()
        try:
            if not release.wait(5):
                raise TimeoutError("Event loop did not release the blocking operation")
            if worker_fails:
                raise failure
            return result if phase == "lock" else operation(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(module, name, block_operation)

    async def check() -> None:
        task = asyncio.create_task(run_workflow(workflow, config, console))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            assert not task.done()
            locks = list((tmp_path / ".crewplane/locks").glob("*"))
            assert bool(locks) == (phase != "lock" or not worker_fails)
            for message in ("first cancellation", "second cancellation"):
                task.cancel(message)
                await asyncio.sleep(0)
                assert not task.done()
                assert list((tmp_path / ".crewplane/locks").glob("*")) == locks
            if phase == "exports":
                (run_dir,) = run_directories(tmp_path)
                running = json.loads(
                    (run_dir / "manifests/run.json").read_text(encoding="utf-8")
                )
                assert running["status"] == "running"
            release.set()
            expected = (
                (typer.Exit if phase == "history" else RuntimeError)
                if worker_fails
                else asyncio.CancelledError
            )
            with pytest.raises(expected) as raised:
                await asyncio.wait_for(task, 5)
            if worker_fails:
                error = raised.value.__cause__ if phase == "history" else raised.value
                assert error is failure
            else:
                assert raised.value.args == ("first cancellation",)
            assert finished.is_set()
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(check())

    assert not list((tmp_path / ".crewplane/locks").glob("*"))
    if phase == "exports":
        (run_dir,) = run_directories(tmp_path)
        manifest = json.loads(
            (run_dir / "manifests/run.json").read_text(encoding="utf-8")
        )
        assert manifest["status"] == ("failed" if worker_fails else "cancelled")
        assert stream.getvalue().count("Run Summary:") == 1
    else:
        assert len(run_directories(tmp_path)) == (1 if phase == "history" else 0)


def test_hydration_failure_terminalizes_with_the_expanded_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    history = RunHistoryRecord(
        manifest=make_run_manifest("source", "workflow--source", status="failed"),
        manifest_path=tmp_path / "history/manifests/run.json",
        run_dir=tmp_path / "history",
        results_dir=tmp_path / "history-results",
    )
    resume_plan = ResumePlan(
        workflow_identity="workflow.task.md",
        decision=ResumeDecision(kind="resume", resume_source=history),
        frontier=ValidatedResumeFrontier(source=history, node_states={}),
    )
    monkeypatch.setattr(execution, "build_resume_plan", Mock(return_value=resume_plan))
    monkeypatch.setattr(execution, "fulfill_historical_branch_exports", Mock())
    failure = RuntimeError("resume copy failed")
    hydrated_plans: list[PreflightExecutionPlan] = []

    def expand_plan(
        frontier: object, plan: PreflightExecutionPlan, output: object
    ) -> PreflightExecutionPlan:
        del frontier, output
        expanded = plan.model_copy(
            update={
                "nodes": [
                    *plan.nodes,
                    plan.nodes[0].model_copy(update={"id": "restored"}),
                ]
            }
        )
        hydrated_plans.append(expanded)
        return expanded

    terminalize = Mock(wraps=execution.terminalize_component_setup_failure)
    components = Mock()
    checkpoints = Mock()
    monkeypatch.setattr(execution, "hydrate_expansions", expand_plan)
    monkeypatch.setattr(execution, "hydrate_resume_frontier", Mock(side_effect=failure))
    monkeypatch.setattr(execution, "hydrate_review_checkpoints", checkpoints)
    monkeypatch.setattr(execution, "build_components_for_run", components)
    monkeypatch.setattr(execution, "terminalize_component_setup_failure", terminalize)

    with pytest.raises(RuntimeError, match="resume copy failed") as raised:
        asyncio.run(
            run_workflow(
                runner_workflow(),
                mock_runner_config(),
                Console(file=io.StringIO(), force_terminal=False),
            )
        )

    assert raised.value is failure
    assert terminalize.call_args.args[1] is hydrated_plans[0]
    components.assert_not_called()
    checkpoints.assert_not_called()
    (run_dir,) = run_directories(tmp_path)
    manifest = json.loads((run_dir / "manifests/run.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["failure_message"] == "resume copy failed"
    assert "- Status: failed" in (run_dir / "logs/summary.md").read_text(
        encoding="utf-8"
    )
    assert not list((tmp_path / ".crewplane/locks").iterdir())


@pytest.mark.race
@pytest.mark.parametrize(
    "phase", ["snapshot", "compilation", "materialization", "hydration"]
)
@pytest.mark.parametrize("worker_fails", [False, True])
def test_blocking_setup_settles_before_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, worker_fails: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    entered, release, finished = Event(), Event(), Event()
    failure = RuntimeError("setup operation failed")
    config = mock_runner_config()
    workflow = runner_workflow()
    if phase == "materialization":
        (tmp_path / "input.txt").write_bytes(b"static input\n")
        workflow = runner_workflow("{{file:input.txt}}")
    console = Console(file=io.StringIO(), force_terminal=False)
    operations = {
        "snapshot": "build_runtime_config_snapshot",
        "compilation": "compile_preview",
        "materialization": "materialize_preflight_success",
        "hydration": "hydrate_resume_frontier",
    }
    if phase == "hydration":
        history = RunHistoryRecord(
            manifest=make_run_manifest("source", "workflow--source", status="failed"),
            manifest_path=tmp_path / "history/manifests/run.json",
            run_dir=tmp_path / "history",
            results_dir=tmp_path / "history-results",
        )
        resume = ResumePlan(
            workflow_identity="workflow.task.md",
            decision=ResumeDecision(kind="resume", resume_source=history),
            frontier=ValidatedResumeFrontier(source=history, node_states={}),
        )
        monkeypatch.setattr(execution, "build_resume_plan", Mock(return_value=resume))
        monkeypatch.setattr(execution, "fulfill_historical_branch_exports", Mock())
    operation = getattr(execution, operations[phase])

    def block_operation(*args: object, **kwargs: object) -> object:
        entered.set()
        try:
            if not release.wait(5):
                raise TimeoutError("Event loop did not release setup work")
            if worker_fails:
                raise failure
            return operation(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(execution, operations[phase], block_operation)
    scheduler = Mock()

    async def check() -> None:
        task = asyncio.create_task(
            run_workflow(workflow, config, console, execute_workflow_impl=scheduler)
        )
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            assert not task.done()
            locks = list((tmp_path / ".crewplane/locks").glob("*"))
            assert bool(locks) == (phase in {"materialization", "hydration"})
            for message in ("first cancellation", "second cancellation"):
                task.cancel(message)
                await asyncio.sleep(0)
                assert not task.done()
                assert list((tmp_path / ".crewplane/locks").glob("*")) == locks
            if phase == "materialization":
                (run_dir,) = run_directories(tmp_path)
                assert not (run_dir / "manifests/run.json").exists()
            release.set()
            expected = (
                (typer.Exit if phase == "snapshot" else RuntimeError)
                if worker_fails
                else asyncio.CancelledError
            )
            with pytest.raises(expected) as raised:
                await asyncio.wait_for(task, 5)
            if worker_fails:
                error = raised.value.__cause__ if phase == "snapshot" else raised.value
                assert error is failure
            else:
                assert raised.value.args == ("first cancellation",)
            assert finished.is_set()
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(check())
    scheduler.assert_not_called()
    assert not list((tmp_path / ".crewplane/locks").glob("*"))
    directories = run_directories(tmp_path)
    if phase == "hydration":
        (run_dir,) = directories
        manifest = json.loads(
            (run_dir / "manifests/run.json").read_text(encoding="utf-8")
        )
        assert manifest["status"] == ("failed" if worker_fails else "cancelled")
        if not worker_fails:
            assert manifest["cancel_reason"] == "external_cancellation"
    elif phase == "materialization":
        (run_dir,) = directories
        assert not (run_dir / "manifests/run.json").exists()
        plan_path = run_dir / "preflight/execution-plan.json"
        assert plan_path.exists() == (not worker_fails)
        if not worker_fails:
            plan = json.loads(plan_path.read_bytes())
            (resource,) = plan["static_resources"]
            assert (run_dir / "preflight" / resource["content_ref"]).read_bytes() == (
                b"static input\n"
            )
    else:
        assert len(directories) == int(phase == "snapshot" and worker_fails)


@pytest.mark.race
@pytest.mark.parametrize("phase", ["lock", "history", "materialization", "exports"])
@pytest.mark.parametrize("worker_fails", [False, True])
def test_runner_shutdown_settles_run_work_before_releasing_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, worker_fails: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    entered, release, finished = Event(), Event(), Event()
    console = Console(file=io.StringIO(), force_terminal=False)
    config, workflow = mock_runner_config(), runner_workflow()
    failure = RuntimeError("shutdown worker failed")
    if phase == "history":
        asyncio.run(run_workflow(workflow, config, console))
        module, name = execution_helpers, "fulfill_branch_exports_from_history"
    else:
        module = execution
        name = {
            "lock": "acquire_same_context_lock",
            "materialization": "materialize_preflight_success",
            "exports": "fulfill_branch_exports",
        }[phase]
    operation = getattr(module, name)

    def block_operation(*args: object, **kwargs: object) -> object:
        result = (
            operation(*args, **kwargs) if phase == "lock" and not worker_fails else None
        )
        entered.set()
        try:
            if not release.wait(5):
                raise TimeoutError("Runner shutdown did not release run work")
            if worker_fails:
                raise failure
            return result if phase == "lock" else operation(*args, **kwargs)
        finally:
            finished.set()

    settlement_at_release: list[bool] = []
    original_release = SameContextLock.release

    def record_release(
        lock: SameContextLock,
        terminal_complete: bool = True,
        cleanup_unconfirmed: bool = False,
    ) -> None:
        settlement_at_release.append(finished.is_set())
        original_release(lock, terminal_complete, cleanup_unconfirmed)

    monkeypatch.setattr(module, name, block_operation)
    monkeypatch.setattr(SameContextLock, "release", record_release)

    async def start_run() -> asyncio.Task[None]:
        loop = asyncio.get_running_loop()

        async def release_on_shutdown() -> None:
            try:
                await asyncio.Future[None]()
            finally:
                loop.call_soon(loop.call_soon, release.set)

        task = asyncio.create_task(run_workflow(workflow, config, console))
        probe = asyncio.create_task(release_on_shutdown())
        assert await asyncio.to_thread(entered.wait, 5)
        assert not probe.done()
        return task

    try:
        with asyncio.Runner() as runner:
            task = runner.run(start_run())
    finally:
        release.set()

    assert finished.is_set()
    assert settlement_at_release == ([] if phase == "lock" and worker_fails else [True])
    assert not list((tmp_path / ".crewplane/locks").glob("*"))
    if worker_fails:
        assert not task.cancelled()
        error = task.exception()
        assert (error.__cause__ if phase == "history" else error) is failure
    else:
        assert task.cancelled()
    if phase == "materialization":
        (run_dir,) = run_directories(tmp_path)
        assert not (run_dir / "manifests/run.json").exists()
        assert (run_dir / "preflight/execution-plan.json").exists() == (
            not worker_fails
        )
    if phase == "exports":
        (run_dir,) = run_directories(tmp_path)
        manifest = json.loads(
            (run_dir / "manifests/run.json").read_text(encoding="utf-8")
        )
        assert manifest["status"] == ("failed" if worker_fails else "cancelled")
