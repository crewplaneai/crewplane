import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.architecture.contracts import (
    ChildProcessEnvironment,
    InvocationContext,
    InvocationProcessEvent,
)
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.agent.invocation.command import (
    run_command_once,
)
from crewplane.runtime.agent.workspace_environment import workspace_child_environment
from tests.integration.runtime.agent.invocation_command_support import (
    command_workspace_context,
)


class InvocationCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_command_once_emits_started_and_exited_process_events(
        self,
    ) -> None:
        events: list[InvocationProcessEvent] = []
        context = InvocationContext(
            node_id="node.a",
            task_id="generic_executor_0",
            provider="generic",
            role=ProviderRole.EXECUTOR,
            process_event_sink=events.append,
        )

        result = await run_command_once(
            cmd=[sys.executable, "-c", "print('ok')"],
            stdin_data=None,
            log_file=None,
            append_log=False,
            log_header=None,
            cwd=Path.cwd(),
            invocation_context=context,
            idle_timeout_seconds=None,
        )
        try:
            assert [event.status for event in events] == ["started", "exited"]
            assert events[0].pid == events[1].pid
            assert [event.attempt for event in events] == [1, 1]
            assert events[0].returncode is None
            assert events[1].returncode == 0
            expected_group_id = events[0].pid if os.name == "posix" else None
            assert events[0].process_group_id == expected_group_id
            assert events[1].process_group_id == expected_group_id
        finally:
            result.cleanup_stream_files()

    async def test_run_command_once_disables_unsupported_process_groups(self) -> None:
        events: list[InvocationProcessEvent] = []
        context = InvocationContext(
            node_id="node.a",
            task_id="generic_executor_0",
            provider="generic",
            role=ProviderRole.EXECUTOR,
            process_event_sink=events.append,
        )

        with patch(
            "crewplane.runtime.agent.invocation.command.supports_posix_process_groups",
            return_value=False,
        ):
            result = await run_command_once(
                cmd=[sys.executable, "-c", "print('ok')"],
                stdin_data=None,
                log_file=None,
                append_log=False,
                log_header=None,
                cwd=Path.cwd(),
                invocation_context=context,
                idle_timeout_seconds=None,
            )
        try:
            assert result.returncode == 0
            assert [event.process_group_id for event in events] == [None, None]
        finally:
            result.cleanup_stream_files()

    async def test_run_command_once_does_not_look_up_spawned_process_group(
        self,
    ) -> None:
        if os.name != "posix":
            self.skipTest("process groups are POSIX-only")

        with patch(
            "crewplane.runtime.agent.invocation.command.os.getpgid",
            side_effect=AssertionError("racy process-group lookup"),
        ):
            result = await run_command_once(
                cmd=[sys.executable, "-c", "pass"],
                stdin_data=None,
                log_file=None,
                append_log=False,
                log_header=None,
                cwd=Path.cwd(),
                invocation_context=None,
                idle_timeout_seconds=None,
            )

        self.addCleanup(result.cleanup_stream_files)
        assert result.returncode == 0

    async def test_run_command_once_drains_output_while_sending_large_stdin(
        self,
    ) -> None:
        stdin_data = b"x" * (2 * 1024 * 1024)

        result = await asyncio.wait_for(
            run_command_once(
                cmd=[
                    sys.executable,
                    "-c",
                    (
                        "import sys\n"
                        "while chunk := sys.stdin.buffer.read(4096):\n"
                        "    sys.stdout.buffer.write(chunk)\n"
                        "    sys.stdout.buffer.flush()\n"
                    ),
                ],
                stdin_data=stdin_data,
                log_file=None,
                append_log=False,
                log_header=None,
                cwd=Path.cwd(),
                invocation_context=None,
                idle_timeout_seconds=None,
            ),
            timeout=2.0,
        )
        try:
            assert result.returncode == 0
            assert result.stdout_path.read_bytes() == stdin_data
            assert result.stderr_path.read_bytes() == b""
        finally:
            result.cleanup_stream_files()

    async def test_run_command_once_applies_cwd_and_child_environment(self) -> None:
        with patch.dict(os.environ, {"WORKSPACE_TEST_UNSET": "inherited"}):
            result = await run_command_once(
                cmd=[
                    sys.executable,
                    "-c",
                    (
                        "import os, pathlib; "
                        "print(pathlib.Path.cwd()); "
                        "print(os.getenv('WORKSPACE_TEST_SET')); "
                        "print(os.getenv('WORKSPACE_TEST_UNSET'))"
                    ),
                ],
                stdin_data=None,
                log_file=None,
                append_log=False,
                log_header=None,
                cwd=Path.cwd(),
                invocation_context=None,
                idle_timeout_seconds=None,
                child_environment=ChildProcessEnvironment(
                    set={"WORKSPACE_TEST_SET": "applied"},
                    unset=("WORKSPACE_TEST_UNSET",),
                ),
            )

        self.addCleanup(result.cleanup_stream_files)
        lines = result.stdout_text.strip().splitlines()
        assert result.returncode == 0
        assert Path(lines[0]) == Path.cwd()
        assert lines[1] == "applied"
        assert lines[2] == "None"

    async def test_workspace_child_environment_preserves_git_transport_controls(
        self,
    ) -> None:
        inherited = {
            "GIT_PROTOCOL_FROM_USER": "0",
            "GIT_ALLOW_PROTOCOL": "https",
        }
        with patch.dict(os.environ, inherited):
            result = await run_command_once(
                cmd=[
                    sys.executable,
                    "-c",
                    (
                        "import os; "
                        "print(os.getenv('GIT_PROTOCOL_FROM_USER')); "
                        "print(os.getenv('GIT_ALLOW_PROTOCOL'))"
                    ),
                ],
                stdin_data=None,
                log_file=None,
                append_log=False,
                log_header=None,
                cwd=Path.cwd(),
                invocation_context=None,
                idle_timeout_seconds=None,
                child_environment=workspace_child_environment(Path.cwd()),
            )

        self.addCleanup(result.cleanup_stream_files)
        assert result.stdout_text.strip().splitlines() == ["0", "https"]

    async def test_run_command_once_records_child_environment_after_spawn(self) -> None:
        record_calls = 0

        def record_child_environment_applied() -> None:
            nonlocal record_calls
            record_calls += 1

        result = await run_command_once(
            cmd=[sys.executable, "-c", "print('ok')"],
            stdin_data=None,
            log_file=None,
            append_log=False,
            log_header=None,
            cwd=Path.cwd(),
            invocation_context=command_workspace_context(
                Path.cwd(),
                record_child_environment_applied,
            ),
            idle_timeout_seconds=None,
            child_environment=ChildProcessEnvironment(set={}, unset=()),
        )

        self.addCleanup(result.cleanup_stream_files)
        assert result.returncode == 0
        assert record_calls == 1

    async def test_run_command_once_does_not_record_child_environment_when_spawn_fails(
        self,
    ) -> None:
        record_calls = 0

        def record_child_environment_applied() -> None:
            nonlocal record_calls
            record_calls += 1

        with pytest.raises(RuntimeError, match="CLI executable not found"):
            await run_command_once(
                cmd=[str(Path.cwd() / "definitely-missing-provider-cli")],
                stdin_data=None,
                log_file=None,
                append_log=False,
                log_header=None,
                cwd=Path.cwd(),
                invocation_context=command_workspace_context(
                    Path.cwd(),
                    record_child_environment_applied,
                ),
                idle_timeout_seconds=None,
                child_environment=ChildProcessEnvironment(set={}, unset=()),
            )

        assert record_calls == 0
