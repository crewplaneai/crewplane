import asyncio
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from crewplane.architecture.contracts import (
    InvocationContext,
    InvocationProcessEvent,
)
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.agent.invocation.command import (
    run_command_once,
)
from crewplane.runtime.agent.process.drain import (
    ProcessDrainError,
    ProcessDrainEvidence,
)
from crewplane.runtime.workspace.mutator_fence import (
    fence_workspace_mutator,
    release_workspace_mutator,
    workspace_mutator_is_fenced,
)
from tests.integration.runtime.agent.invocation_command_support import (
    command_workspace_context,
)


class InvocationCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_command_records_confirmed_process_drain(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "workspace-state.json"
            state_path.write_text(
                json.dumps({"process_drain": {"status": "not_started"}}),
                encoding="utf-8",
            )
            fence_workspace_mutator(state_path)
            events: list[InvocationProcessEvent] = []
            context = command_workspace_context(Path.cwd(), lambda: None)
            assert context.workspace is not None
            context = replace(
                context,
                process_event_sink=events.append,
                workspace=replace(
                    context.workspace,
                    workspace_state_path=state_path,
                ),
            )
            task = asyncio.create_task(
                run_command_once(
                    cmd=[sys.executable, "-c", "import time; time.sleep(30)"],
                    stdin_data=None,
                    log_file=None,
                    append_log=False,
                    log_header=None,
                    cwd=Path.cwd(),
                    invocation_context=context,
                    idle_timeout_seconds=None,
                )
            )
            try:
                async with asyncio.timeout(5.0):
                    while not events:
                        if task.done():
                            await task
                            self.fail("Command completed without a process event.")
                        await asyncio.sleep(0.01)

                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

                payload = json.loads(state_path.read_text(encoding="utf-8"))
                assert payload["process_drain"]["status"] == "confirmed"
                assert not workspace_mutator_is_fenced(state_path)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                release_workspace_mutator(state_path)

    async def test_cancelled_command_records_unresolved_process_drain(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "workspace-state.json"
            state_path.write_text(
                json.dumps({"process_drain": {"status": "not_started"}}),
                encoding="utf-8",
            )
            collection_started = asyncio.Event()
            context = command_workspace_context(Path.cwd(), lambda: None)
            assert context.workspace is not None
            context = replace(
                context,
                workspace=replace(
                    context.workspace,
                    workspace_state_path=state_path,
                ),
            )

            async def block_collection(*args: object, **kwargs: object) -> None:
                del args, kwargs
                collection_started.set()
                await asyncio.Event().wait()

            async def fail_drain(
                process: asyncio.subprocess.Process,
                process_group_id: int | None,
                diagnostic_sink: object,
            ) -> None:
                del diagnostic_sink
                process.kill()
                await process.wait()
                raise ProcessDrainError(
                    ProcessDrainEvidence(
                        pid=process.pid,
                        process_group_id=process_group_id,
                        leader_stopped=True,
                        process_group_stopped=False,
                    ),
                    "provider process group remained live",
                )

            with (
                patch(
                    "crewplane.runtime.agent.invocation.command."
                    "write_stdin_and_collect_output",
                    new=block_collection,
                ),
                patch(
                    "crewplane.runtime.agent.invocation.command.reap_failed_process",
                    new=fail_drain,
                ),
            ):
                task = asyncio.create_task(
                    run_command_once(
                        cmd=[sys.executable, "-c", "import time; time.sleep(30)"],
                        stdin_data=None,
                        log_file=None,
                        append_log=False,
                        log_header=None,
                        cwd=Path.cwd(),
                        invocation_context=context,
                        idle_timeout_seconds=None,
                    )
                )
                await asyncio.wait_for(collection_started.wait(), timeout=1.0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError) as caught:
                    await task

            assert "provider process group remained live" in getattr(
                caught.value, "__notes__", ()
            )
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            assert payload["process_drain"]["status"] == "unresolved"
            assert workspace_mutator_is_fenced(state_path)
            release_workspace_mutator(state_path)

    async def test_process_drain_write_failure_preserves_error_and_fence(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "workspace-state.json"
            state_path.write_text(
                json.dumps({"process_drain": {"status": "not_started"}}),
                encoding="utf-8",
            )
            context = command_workspace_context(Path.cwd(), lambda: None)
            assert context.workspace is not None
            context = replace(
                context,
                workspace=replace(
                    context.workspace,
                    workspace_state_path=state_path,
                ),
            )

            async def fail_collection(*args: object, **kwargs: object) -> None:
                del args, kwargs
                raise RuntimeError("provider output collection failed")

            async def fail_drain(
                process: asyncio.subprocess.Process,
                process_group_id: int | None,
                diagnostic_sink: object,
            ) -> None:
                del diagnostic_sink
                process.kill()
                await process.wait()
                raise ProcessDrainError(
                    ProcessDrainEvidence(
                        pid=process.pid,
                        process_group_id=process_group_id,
                        leader_stopped=True,
                        process_group_stopped=False,
                    ),
                    "provider process group remained live",
                )

            with (
                patch(
                    "crewplane.runtime.agent.invocation.command."
                    "write_stdin_and_collect_output",
                    new=fail_collection,
                ),
                patch(
                    "crewplane.runtime.agent.invocation.command.reap_failed_process",
                    new=fail_drain,
                ),
                patch(
                    "crewplane.runtime.workspace.state_evidence."
                    "record_workspace_process_drain",
                    side_effect=OSError("transient state write failure"),
                ),
                pytest.raises(ProcessDrainError) as caught,
            ):
                await run_command_once(
                    cmd=[sys.executable, "-c", "import time; time.sleep(30)"],
                    stdin_data=None,
                    log_file=None,
                    append_log=False,
                    log_header=None,
                    cwd=Path.cwd(),
                    invocation_context=context,
                    idle_timeout_seconds=None,
                )

            assert (
                "Workspace process-drain evidence persistence failed: "
                "transient state write failure"
                in getattr(caught.value, "__notes__", ())
            )
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            assert payload["process_drain"]["status"] == "not_started"
            assert workspace_mutator_is_fenced(state_path)
            release_workspace_mutator(state_path)

    async def test_cancellation_reports_process_exit_after_reaping(self) -> None:
        events: list[InvocationProcessEvent] = []
        process_started = asyncio.Event()

        def record_process_event(event: InvocationProcessEvent) -> None:
            events.append(event)
            if event.status == "started":
                process_started.set()

        context = InvocationContext(
            node_id="node.a",
            task_id="generic_executor_0",
            provider="generic",
            role=ProviderRole.EXECUTOR,
            process_event_sink=record_process_event,
        )
        invocation = asyncio.create_task(
            run_command_once(
                cmd=[sys.executable, "-c", "import time; time.sleep(10)"],
                stdin_data=None,
                log_file=None,
                append_log=False,
                log_header=None,
                cwd=Path.cwd(),
                invocation_context=context,
                idle_timeout_seconds=None,
            )
        )
        await asyncio.wait_for(process_started.wait(), timeout=1.0)

        invocation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await invocation

        assert [event.status for event in events] == ["started", "exited"]
        assert events[1].returncode is not None
