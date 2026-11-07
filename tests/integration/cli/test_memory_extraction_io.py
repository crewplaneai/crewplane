from __future__ import annotations

import asyncio
from contextvars import ContextVar
from pathlib import Path
from threading import Event, get_ident
from unittest.mock import AsyncMock, Mock

import pytest

from crewplane.artifacts.memory.evidence import NoEligibleEvidence
from crewplane.artifacts.memory.receipts import extraction_attempt, read_extraction
from crewplane.cli.memory import extraction
from crewplane.cli.run import passes
from crewplane.cli.run.observability import WorkflowCancelledByUser
from crewplane.cli.workflow_context import CliWorkflowContext, CliWorkflowPaths
from crewplane.core.preflight.source import PreflightWorkflowSource
from scripts.validation_policy import load_policy
from tests.helpers.memory import lesson_document
from tests.helpers.memory_runtime import setup_parent
from tests.integration.cli.workflow_runner_support import runner_workflow

TIMEOUTS = load_policy().timeouts


def test_initial_receipt_failure_propagates_before_child_execution(
    tmp_path: Path, monkeypatch
):
    config, parent, _packet, _fixtures, console = setup_parent(tmp_path)
    error = OSError("initial receipt unavailable")
    execute = AsyncMock()
    manifest = (parent.stages_dir / "manifests/run.json").read_bytes()

    def fail_save(_attempt, _receipt):
        del _attempt, _receipt
        raise error

    monkeypatch.setattr(extraction, "save_extraction", fail_save)
    monkeypatch.setattr(extraction, "execute_workflow_run", execute)
    with pytest.raises(OSError) as captured:
        asyncio.run(extraction.extract_memory(config, parent, console))
    assert captured.value is error
    execute.assert_not_awaited()
    assert read_extraction(parent.stages_dir) is None
    assert (parent.stages_dir / "manifests/run.json").read_bytes() == manifest


@pytest.mark.race
@pytest.mark.parametrize("cancel_count", [0, 1, 2])
def test_empty_evidence_capture_preserves_skip_or_cancellation(
    tmp_path: Path, monkeypatch, cancel_count
):
    config, parent, _packet, _fixtures, console = setup_parent(tmp_path)
    release = Event()
    completed = Event()
    execute = AsyncMock()
    error = NoEligibleEvidence("No usable retained evidence.")
    manifest = (parent.stages_dir / "manifests/run.json").read_bytes()
    monkeypatch.setattr(extraction, "execute_workflow_run", execute)

    async def scenario():
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()

        def capture(*_args):
            del _args
            loop.call_soon_threadsafe(entered.set)
            try:
                assert release.wait(TIMEOUTS.scenario_seconds), "worker not released"
                raise error
            finally:
                completed.set()

        monkeypatch.setattr(extraction, "capture_evidence", capture)
        task = asyncio.create_task(extraction.extract_memory(config, parent, console))
        try:
            async with asyncio.timeout(TIMEOUTS.scenario_seconds):
                await asyncio.wait_for(entered.wait(), TIMEOUTS.readiness_seconds)
                for index in range(cancel_count):
                    task.cancel(f"cancel empty capture {index}")
                    await asyncio.sleep(0)
                    assert not task.done()
                assert not completed.is_set()
                with pytest.raises(TimeoutError):
                    async with extraction_attempt(parent.stages_dir):
                        pytest.fail("Evidence capture lost its owning lock")
                release.set()
                if cancel_count:
                    with pytest.raises(asyncio.CancelledError) as captured:
                        await task
                    assert (
                        str(captured.value)
                        == f"cancel empty capture {cancel_count - 1}"
                    )
                    assert task.cancelled()
                else:
                    result = await task
                    assert result == extraction.ExtractionResult(message=str(error))
        finally:
            release.set()
            task.cancel()
            await asyncio.wait_for(
                asyncio.gather(task, return_exceptions=True), TIMEOUTS.cleanup_seconds
            )
        assert completed.is_set()
        async with extraction_attempt(parent.stages_dir) as attempt:
            assert attempt.active

    asyncio.run(scenario())
    execute.assert_not_awaited()
    assert read_extraction(parent.stages_dir) is None
    assert (parent.stages_dir / "manifests/run.json").read_bytes() == manifest


@pytest.mark.race
@pytest.mark.parametrize("regenerate", [False, True])
def test_cancelled_initial_receipt_remains_retryable(
    tmp_path: Path, monkeypatch, regenerate
):
    config, parent, _packet, fixtures, console = setup_parent(tmp_path)
    (fixtures / "extract.md").write_text(
        f"```crewplane-memory-candidates\n{lesson_document().model_dump_json()}\n```",
        encoding="utf-8",
        newline="\n",
    )
    release = Event()
    original = extraction.save_extraction
    execute = AsyncMock(wraps=extraction.execute_workflow_run)
    monkeypatch.setattr(extraction, "execute_workflow_run", execute)
    manifest = (parent.stages_dir / "manifests/run.json").read_bytes()

    async def scenario():
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()

        def save(attempt, receipt):
            if receipt.provider_status == "not_started":
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(TIMEOUTS.scenario_seconds), "worker not released"
            original(attempt, receipt)

        monkeypatch.setattr(extraction, "save_extraction", save)
        task = asyncio.create_task(extraction.extract_memory(config, parent, console))
        try:
            async with asyncio.timeout(TIMEOUTS.scenario_seconds):
                await asyncio.wait_for(entered.wait(), TIMEOUTS.readiness_seconds)
                task.cancel("cancel initial receipt")
                await asyncio.sleep(0)
                assert not task.done()
                release.set()
                with pytest.raises(
                    asyncio.CancelledError, match="cancel initial receipt"
                ):
                    await task
                assert task.cancelled()
        finally:
            release.set()
            task.cancel()
            await asyncio.wait_for(
                asyncio.gather(task, return_exceptions=True), TIMEOUTS.cleanup_seconds
            )
        execute.assert_not_awaited()
        receipt = read_extraction(parent.stages_dir)
        assert receipt.cleanup_confirmed is True
        assert receipt.lock_released is True
        assert receipt.extraction_run_id is None
        assert receipt.extraction_run_key is None
        assert not receipt.completion_eligible
        async with extraction_attempt(parent.stages_dir) as attempt:
            assert attempt.active

        monkeypatch.setattr(extraction, "save_extraction", original)
        result = await extraction.extract_memory(config, parent, console, regenerate)
        assert result.message.endswith("no useful lesson proposed.")
        execute.assert_awaited_once()
        assert read_extraction(parent.stages_dir).completion_eligible

    asyncio.run(scenario())
    assert (parent.stages_dir / "manifests/run.json").read_bytes() == manifest


def test_extraction_artifact_io_runs_off_event_loop(tmp_path: Path, monkeypatch):
    config, parent, _packet, fixtures, console = setup_parent(tmp_path)
    (fixtures / "extract.md").write_text(
        f"```crewplane-memory-candidates\n{lesson_document().model_dump_json()}\n```",
        encoding="utf-8",
        newline="\n",
    )
    context = ContextVar("extraction-owner", default="missing")
    observed = []

    async def scenario():
        context.set("caller")
        thread = get_ident()

        def guard(name, operation):
            def checked(*args, **kwargs):
                assert get_ident() != thread, f"{name} blocked the event loop"
                assert context.get() == "caller"
                observed.append(name)
                return operation(*args, **kwargs)

            return checked

        for name in (
            "capture_evidence",
            "read_extraction",
            "extraction_source",
            "save_extraction",
            "retained_extraction_result",
        ):
            monkeypatch.setattr(
                extraction, name, guard(name, getattr(extraction, name))
            )
        first = await extraction.extract_memory(config, parent, console)
        repeated = await extraction.extract_memory(config, parent, console)
        assert first == repeated
        assert first.message.endswith("no useful lesson proposed.")

    asyncio.run(scenario())
    assert set(observed) == {
        "capture_evidence",
        "read_extraction",
        "extraction_source",
        "save_extraction",
        "retained_extraction_result",
    }


@pytest.mark.race
@pytest.mark.parametrize(
    "phase",
    [
        "capture_evidence",
        "read_extraction",
        "extraction_source",
        "initial_receipt",
        "terminal_receipt",
        "validation_receipt",
        "import_receipt",
        "retained_output",
        "reused_output",
        "failure_receipt",
    ],
)
@pytest.mark.parametrize("storage_failure", [False, True])
def test_cancelled_extraction_io_finishes_before_unlocking(
    tmp_path: Path, monkeypatch, phase, storage_failure
):
    config, parent, _packet, fixtures, console = setup_parent(tmp_path)
    (fixtures / "extract.md").write_text(
        f"```crewplane-memory-candidates\n{lesson_document().model_dump_json()}\n```",
        encoding="utf-8",
        newline="\n",
    )
    if phase == "reused_output":
        asyncio.run(extraction.extract_memory(config, parent, console))
    cancellation = WorkflowCancelledByUser()
    if phase == "failure_receipt":
        monkeypatch.setattr(
            extraction, "execute_workflow_run", AsyncMock(side_effect=cancellation)
        )
    function = {
        "initial_receipt": "save_extraction",
        "terminal_receipt": "save_extraction",
        "validation_receipt": "save_extraction",
        "import_receipt": "save_extraction",
        "failure_receipt": "save_extraction",
        "retained_output": "retained_extraction_result",
        "reused_output": "retained_extraction_result",
    }.get(phase, phase)
    original = getattr(extraction, function)
    release = Event()
    completed = Event()
    error = OSError("extraction I/O failed")
    manifest = (parent.stages_dir / "manifests/run.json").read_bytes()

    async def scenario():
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()
        thread = get_ident()

        def blocked_io(*args, **kwargs):
            if function == "save_extraction":
                attempt, receipt = args
                selected = {
                    "initial_receipt": receipt.provider_status == "not_started",
                    "terminal_receipt": receipt.provider_status == "succeeded"
                    and receipt.validation_status == "not_attempted",
                    "validation_receipt": receipt.validation_status == "succeeded"
                    and receipt.import_status == "not_attempted",
                    "import_receipt": receipt.import_status == "succeeded",
                    "failure_receipt": receipt.provider_status == "failed",
                }[phase]
                if not selected:
                    return original(*args, **kwargs)
                assert attempt.active
            assert get_ident() != thread, "Extraction I/O blocked the event loop"
            loop.call_soon_threadsafe(entered.set)
            try:
                assert release.wait(TIMEOUTS.scenario_seconds), "worker not released"
                if storage_failure:
                    raise error
                return original(*args, **kwargs)
            finally:
                completed.set()

        monkeypatch.setattr(extraction, function, blocked_io)
        task = asyncio.create_task(extraction.extract_memory(config, parent, console))
        try:
            async with asyncio.timeout(TIMEOUTS.scenario_seconds):
                await asyncio.wait_for(entered.wait(), TIMEOUTS.readiness_seconds)
                for message in ("first cancellation", "second cancellation"):
                    task.cancel(message)
                    await asyncio.sleep(0)
                    assert not task.done()
                    assert not completed.is_set()
                    with pytest.raises(TimeoutError):
                        async with extraction_attempt(parent.stages_dir):
                            pytest.fail("Extraction I/O lost its owning lock")
                release.set()
                if phase == "failure_receipt":
                    with pytest.raises(WorkflowCancelledByUser) as captured:
                        await task
                    assert captured.value is cancellation
                else:
                    with pytest.raises(asyncio.CancelledError) as captured:
                        await task
                    if storage_failure:
                        assert captured.value.__cause__ is error
        finally:
            release.set()
            task.cancel()
            await asyncio.wait_for(
                asyncio.gather(task, return_exceptions=True), TIMEOUTS.cleanup_seconds
            )
        assert completed.is_set()
        async with extraction_attempt(parent.stages_dir) as attempt:
            assert attempt.active

    asyncio.run(scenario())
    assert (parent.stages_dir / "manifests/run.json").read_bytes() == manifest
    if phase == "failure_receipt" and not storage_failure:
        assert read_extraction(parent.stages_dir).provider_status == "failed"


@pytest.mark.race
@pytest.mark.parametrize("storage_failure", [False, True])
def test_runner_shutdown_drains_extraction_io_before_attempt_cleanup(
    tmp_path: Path, monkeypatch, storage_failure
):
    config, parent, _packet, _fixtures, console = setup_parent(tmp_path)
    release = Event()
    completed = Event()
    error = OSError("evidence read failed during shutdown")
    cleanup = []
    errors = []
    original_capture = extraction.capture_evidence

    async def main():
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()

        def capture(*args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            try:
                assert release.wait(TIMEOUTS.scenario_seconds), "worker not released"
                if storage_failure:
                    raise error
                return original_capture(*args, **kwargs)
            finally:
                completed.set()

        monkeypatch.setattr(extraction, "capture_evidence", capture)

        async def owner():
            try:
                await extraction.extract_memory(config, parent, console)
            except (OSError, asyncio.CancelledError) as exc:
                errors.append(exc)
            finally:
                cleanup.append(completed.is_set())

        async def release_on_shutdown():
            try:
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                release.set()

        tasks = [
            asyncio.create_task(owner()),
            asyncio.create_task(release_on_shutdown()),
        ]
        await asyncio.wait_for(entered.wait(), TIMEOUTS.readiness_seconds)
        assert all(not task.done() for task in tasks)

    try:
        asyncio.run(main())
    finally:
        release.set()
    assert cleanup == [True]
    assert len(errors) == 1
    assert isinstance(errors[0], asyncio.CancelledError)
    if storage_failure:
        assert errors[0].__cause__ is error

    async def reacquire():
        async with extraction_attempt(parent.stages_dir) as attempt:
            assert attempt.active

    asyncio.run(reacquire())


@pytest.mark.race
def test_cancelled_learning_io_failure_stops_repeated_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, parent, _packet, _fixtures, console = setup_parent(tmp_path)
    workflow = runner_workflow()
    workflow.nodes[0].memory = True
    workflow.repeat_force_run_count = 2
    paths = CliWorkflowPaths(
        tmp_path / "workflow.task.md",
        tmp_path / "config.yml",
        tmp_path,
        parent.state_dir,
    )
    context = CliWorkflowContext(
        paths,
        config,
        PreflightWorkflowSource.from_workflow(workflow),
    )
    execute = AsyncMock(return_value=parent)
    monkeypatch.setattr(passes, "load_cli_workflow_context", Mock(return_value=context))
    release = Event()
    error = OSError("evidence storage failed")
    manifest = (parent.stages_dir / "manifests/run.json").read_bytes()

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()

        def fail_capture(*args) -> None:
            del args
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(TIMEOUTS.scenario_seconds), "worker not released"
            raise error

        monkeypatch.setattr(extraction, "capture_evidence", fail_capture)
        task = asyncio.create_task(
            passes.execute_passes(context, False, True, console, execute)
        )
        try:
            async with asyncio.timeout(TIMEOUTS.scenario_seconds):
                await asyncio.wait_for(entered.wait(), TIMEOUTS.readiness_seconds)
                task.cancel("cancel learning")
                await asyncio.sleep(0)
                assert not task.done()
                release.set()
                with pytest.raises(asyncio.CancelledError) as captured:
                    await task
                assert captured.value.__cause__ is error
                assert task.cancelled()
        finally:
            release.set()
            task.cancel()
            await asyncio.wait_for(
                asyncio.gather(task, return_exceptions=True), TIMEOUTS.cleanup_seconds
            )
        execute.assert_awaited_once()
        async with extraction_attempt(parent.stages_dir) as attempt:
            assert attempt.active

    asyncio.run(scenario())
    assert (parent.stages_dir / "manifests/run.json").read_bytes() == manifest
