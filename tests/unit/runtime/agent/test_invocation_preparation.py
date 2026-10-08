import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from crewplane.adapters.invokers.cli_invoker import get_cli_provider_capability
from crewplane.architecture.contracts import (
    CommandResult,
    InvocationContext,
    InvocationPlan,
)
from crewplane.core.config import AgentConfig
from crewplane.runtime.agent.invocation import command, loop
from crewplane.runtime.agent.usage import InvocationUsageAccumulator


@pytest.fixture
def attempt_case(tmp_path, monkeypatch):
    structured = tmp_path / "structured"
    structured.write_text("stale")
    capability = get_cli_provider_capability("generic")
    plan = InvocationPlan(
        cmd=["provider"],
        stdin_data=None,
        structured_output_file=structured,
        output_extractor=None,
        usage_decoder=None,
        quota_classifier=capability.quota_classifier,
        failure_classifier=capability.failure_classifier,
        log_header=b"",
    )
    result = CommandResult(0, "answer", "")
    config = AgentConfig(cli_cmd=["provider"], invocation_timeout_seconds=0.01)
    usages, order = [], []
    main_thread = threading.get_ident()
    context = InvocationContext(
        "node", "task", "provider", "executor", usage_recorder=usages.append
    )
    accounting = Mock()
    original_start = InvocationUsageAccumulator.record_attempt_start

    def record_start(accumulator):
        assert not structured.exists()
        assert threading.get_ident() == main_thread
        order.append("accounting")
        accounting()
        original_start(accumulator)

    monkeypatch.setattr(
        InvocationUsageAccumulator, "record_attempt_start", record_start
    )
    runner = AsyncMock(return_value=result)
    monkeypatch.setattr(loop, "run_invocation_attempt", runner)
    output = tmp_path / "output"

    def invoke():
        return loop.run_invocation_loop(
            config, "prompt", output, None, tmp_path, context, AsyncMock(), plan
        )

    return SimpleNamespace(
        path=structured,
        plan=plan,
        runner=runner,
        result=result,
        invoke=invoke,
        accounting=accounting,
        order=order,
        output=output,
        usages=usages,
    )


@pytest.mark.parametrize("fails", [False, True])
def test_preparation_precedes_attempt_accounting_and_preserves_errors(
    monkeypatch, attempt_case, fails
):
    case = attempt_case
    failure = OSError("unlink failed")

    def prepare(plan):
        assert plan is case.plan
        case.order.append("prepare")
        if fails:
            raise failure
        command.prepare_runtime_for_attempt(plan)

    async def invoke(**kwargs):
        assert case.order == ["prepare", "accounting"]
        assert kwargs["plan"] is case.plan
        assert kwargs["attempt"] == 0
        assert kwargs["timeout_seconds"] == 0.01
        case.order.append("invoke")
        return case.result

    monkeypatch.setattr(loop, "prepare_runtime_for_attempt", prepare)
    case.runner.side_effect = invoke
    if fails:
        with pytest.raises(OSError) as caught:
            asyncio.run(case.invoke())
        assert caught.value is failure
        case.runner.assert_not_awaited()
        case.accounting.assert_not_called()
        assert case.order == ["prepare"] and not case.output.exists()
    else:
        assert asyncio.run(case.invoke()) is None
        assert case.output.read_text() == "answer"
        assert case.order == ["prepare", "accounting", "invoke"]
    assert not case.path.exists()
    assert case.usages[0].attempt_count == (0 if fails else 1)


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("fails", [False, True])
def test_preparation_keeps_loop_responsive_and_finishes_before_cancellation(
    monkeypatch, attempt_case, cancelled, fails
):
    case = attempt_case
    failure = OSError("unlink failed")
    main_thread = threading.get_ident()
    release = threading.Event()
    worker_threads = []

    async def check():
        ready = asyncio.Event()
        event_loop = asyncio.get_running_loop()
        original_unlink = Path.unlink

        def unlink(path, missing_ok=False):
            if path == case.path and missing_ok:
                worker_threads.append(threading.get_ident())
                event_loop.call_soon_threadsafe(ready.set)
                assert release.wait(1), "event loop did not release preparation"
                if fails:
                    raise failure
            original_unlink(path, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", unlink)
        task = asyncio.create_task(case.invoke())
        try:
            await asyncio.wait_for(ready.wait(), 2)
            assert len(worker_threads) == 1
            assert worker_threads[0] != main_thread
            assert not task.done()
            case.runner.assert_not_awaited()
            case.accounting.assert_not_called()
            if cancelled:
                task.cancel("first cancellation")
                await asyncio.sleep(0)
                task.cancel("second cancellation")
                await asyncio.sleep(0)
                assert not task.done()
            release.set()
            if cancelled:
                with pytest.raises(asyncio.CancelledError) as caught:
                    await task
                assert caught.value.args == ("first cancellation",)
                assert caught.value.__cause__ is (failure if fails else None)
                assert not case.usages
            elif fails:
                with pytest.raises(OSError) as caught:
                    await task
                assert caught.value is failure
                assert case.usages[0].attempt_count == 0
            else:
                assert await task is None
                assert case.output.read_text() == "answer"
                assert case.usages[0].attempt_count == 1
                case.runner.assert_awaited_once()
            assert not case.path.exists()
            if cancelled or fails:
                case.runner.assert_not_awaited()
                case.accounting.assert_not_called()
                assert not case.output.exists()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(check())
