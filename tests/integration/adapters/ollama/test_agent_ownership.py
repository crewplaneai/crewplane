import asyncio
import importlib
import json
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path

import pytest

from crewplane.architecture.contracts import InvocationContext
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.agent.invocation.command import run_command_once
from tests.helpers.cli_provider import wait_for_receipt

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures/providers"
pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="POSIX process-tree ownership"
)


def command_request(root, mode, events):
    final = root / "final.txt"
    command = [
        sys.executable,
        str(FIXTURES / "ollama_command_tree.py"),
        mode,
        str(root),
    ]
    return {
        "cmd": [
            sys.executable,
            str(FIXTURES / "ollama_command_wrapper.py"),
            str(final),
        ],
        "stdin_data": json.dumps({"argv": command}).encode(),
        "log_file": None,
        "append_log": False,
        "log_header": None,
        "cwd": root,
        "invocation_context": InvocationContext(
            node_id="tool",
            task_id="tool_0",
            provider="ollama",
            role=ProviderRole.EXECUTOR,
            process_event_sink=events.append,
        ),
        "idle_timeout_seconds": None,
    }


def cleanup_groups(events):
    for event in events:
        if event.process_group_id is not None:
            assert event.process_group_id != os.getpgrp()
            with suppress(ProcessLookupError):
                os.killpg(event.process_group_id, signal.SIGKILL)


async def assert_descendants_gone(root):
    for name in ("child", "grandchild"):
        process_id = json.loads((root / f"{name}.json").read_text())["pid"]
        async with asyncio.timeout(5):
            while True:
                try:
                    os.kill(process_id, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.01)


@pytest.mark.race
def test_inherited_pipe_hang_fails_without_final_and_runtime_drains_tree(
    ollama_package, tmp_path
):
    del ollama_package
    events = []

    async def run():
        result = None
        try:
            result = await asyncio.wait_for(
                run_command_once(**command_request(tmp_path, "parent-exit", events)),
                15,
            )
            assert result.returncode == 2
            assert result.stdout_text == ""
            assert "inherited pipes remained open" in result.stderr_text
            assert not (tmp_path / "final.txt").exists()
            group = events[0].process_group_id
            assert group is not None
            for name in ("parent", "child", "grandchild"):
                receipt = json.loads((tmp_path / f"{name}.json").read_text())
                assert receipt["group"] == group
                assert receipt["session"] == group
            await assert_descendants_gone(tmp_path)
        finally:
            cleanup_groups(events)
            if result is not None:
                result.cleanup_stream_files()

    asyncio.run(run())


@pytest.mark.race
@pytest.mark.parametrize("stop", ["cancel", "timeout"])
def test_outer_cancellation_or_timeout_drains_term_resistant_tool_descendants(
    ollama_package, tmp_path, stop
):
    del ollama_package
    events = []

    async def run():
        task = asyncio.create_task(
            run_command_once(**command_request(tmp_path, "parent-hold", events))
        )
        try:
            await wait_for_receipt(tmp_path, "parent.json")
            if stop == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 15)
            else:
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(task, 0.05)
            assert not (tmp_path / "final.txt").exists()
            await assert_descendants_gone(tmp_path)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            cleanup_groups(events)

    asyncio.run(run())


@pytest.mark.race
def test_coroutine_cancellation_closes_and_reaps_its_direct_child(
    ollama_package, tmp_path
):
    del ollama_package
    executor = importlib.import_module("crewplane_ollama.command_executor")

    async def run():
        task = asyncio.create_task(
            executor.execute_command(
                {
                    "argv": [
                        sys.executable,
                        str(FIXTURES / "ollama_command_tree.py"),
                        "single",
                        str(tmp_path),
                    ]
                }
            )
        )
        process_id = None
        try:
            receipt_path = await wait_for_receipt(tmp_path, "single.json")
            process_id = json.loads(receipt_path.read_text())["pid"]
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            with pytest.raises(ProcessLookupError):
                os.kill(process_id, 0)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if process_id is not None:
                with suppress(ProcessLookupError):
                    os.kill(process_id, signal.SIGKILL)

    asyncio.run(run())
