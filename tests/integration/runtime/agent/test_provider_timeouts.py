from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from crewplane.adapters.invokers.cli_invoker import build_cli_invocation_plan
from crewplane.architecture.contracts import InvocationContext
from crewplane.core.config import AgentConfig
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.agent.invoker import invoke_agent
from tests.helpers.processes import kill_process_group


@pytest.mark.parametrize(
    ("scope", "error_message", "operation"),
    [
        ("wall_clock", "wall-clock timeout reached after 2s", "invocation_timeout"),
        ("idle", "produced no output for 2s", "invocation_idle_timeout"),
    ],
    ids=["wall-clock", "idle"],
)
def test_provider_timeout_reports_diagnostic_and_reaps_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    error_message: str,
    operation: str,
) -> None:
    asyncio.run(_run_timeout(tmp_path, monkeypatch, scope, error_message, operation))


async def _run_timeout(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    error_message: str,
    operation: str,
) -> None:
    processes: list[asyncio.subprocess.Process] = []
    create_process = asyncio.create_subprocess_exec

    async def record_process(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await create_process(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record_process)
    diagnostics = []
    config = AgentConfig(
        cli_cmd=[sys.executable, "-c", "import time; time.sleep(60)"],
        default_model=None,
        model_arg=None,
        invocation_timeout_seconds=2.0 if scope == "wall_clock" else 30.0,
        invocation_idle_timeout_seconds=2.0 if scope == "idle" else None,
    )
    context = InvocationContext(
        node_id="node.a",
        task_id="generic_executor_0",
        provider="generic",
        role=ProviderRole.EXECUTOR,
        round_num=1,
        diagnostics=diagnostics.append,
    )
    output = root / "output.txt"
    try:
        with pytest.raises(RuntimeError, match=error_message):
            async with asyncio.timeout(20):
                await invoke_agent(
                    config=config,
                    model=None,
                    prompt="prompt",
                    output_file=output,
                    cwd=root,
                    log_file=root / "provider.log",
                    invocation_context=context,
                    plan_builder=build_cli_invocation_plan,
                )
        assert not output.exists()
        assert [diagnostic.operation for diagnostic in diagnostics] == [operation]
        if scope == "wall_clock":
            assert diagnostics[0].attributes["timeout_scope"] == scope
        else:
            assert diagnostics[0].message == (
                "Provider invocation produced no stdout or stderr during the idle timeout window."
            )
        # Observe the OS process in the parent; Python startup is part of the timeout.
        assert len(processes) == 1
        assert processes[0].returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(processes[0].pid, 0)
    finally:
        for process in processes:
            kill_process_group(process.pid)
            await asyncio.wait_for(process.wait(), timeout=10)
