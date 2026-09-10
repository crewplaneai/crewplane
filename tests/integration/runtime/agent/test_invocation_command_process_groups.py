import asyncio
import os
import signal
import sys
import time
import unittest
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory

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
)


class InvocationCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_exit_kills_term_ignoring_process_group_member(
        self,
    ) -> None:
        if os.name != "posix":
            self.skipTest("process groups are POSIX-only")
        events: list[InvocationProcessEvent] = []
        diagnostics = []
        context = InvocationContext(
            node_id="node.a",
            task_id="generic_executor_0",
            provider="generic",
            role=ProviderRole.EXECUTOR,
            process_event_sink=events.append,
            diagnostics=diagnostics.append,
        )
        with TemporaryDirectory(prefix="crewplane-process-drain-") as temp_dir:
            child_pid_path = Path(temp_dir) / "child.pid"
            child_script = (
                "import os, signal, sys, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "open(sys.argv[1], 'w', encoding='utf-8').write(str(os.getpid()))\n"
                "time.sleep(30)\n"
            )
            leader_script = (
                "import subprocess, sys, time\n"
                "path = sys.argv[1]\n"
                "subprocess.Popen([sys.executable, '-c', sys.argv[2], path])\n"
                "while True:\n"
                "    try:\n"
                "        open(path, encoding='utf-8').read()\n"
                "        break\n"
                "    except FileNotFoundError:\n"
                "        time.sleep(0.01)\n"
                "print('leader exited')\n"
            )

            started_at = time.monotonic()
            result = await asyncio.wait_for(
                run_command_once(
                    cmd=[
                        sys.executable,
                        "-c",
                        leader_script,
                        child_pid_path.as_posix(),
                        child_script,
                    ],
                    stdin_data=None,
                    log_file=None,
                    append_log=False,
                    log_header=None,
                    cwd=Path.cwd(),
                    invocation_context=context,
                    idle_timeout_seconds=None,
                ),
                timeout=3.0,
            )
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            process_group_id = events[0].process_group_id
            try:
                assert time.monotonic() - started_at < 3.0
                assert result.returncode == 0
                assert result.stdout_text.strip() == "leader exited"
                assert process_group_id is not None
                assert any(
                    diagnostic.operation == "process_pipe_drain_timeout"
                    for diagnostic in diagnostics
                )
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline:
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        break
                    await asyncio.sleep(0.01)
                else:
                    self.fail("TERM-ignoring process-group member survived KILL")
            finally:
                result.cleanup_stream_files()
                if process_group_id is not None:
                    with suppress(ProcessLookupError):
                        os.killpg(process_group_id, signal.SIGKILL)

    async def test_normal_exit_fails_when_escaped_child_keeps_pipes_open(
        self,
    ) -> None:
        if os.name != "posix":
            self.skipTest("process groups are POSIX-only")
        diagnostics = []
        context = InvocationContext(
            node_id="node.a",
            task_id="generic_executor_0",
            provider="generic",
            role=ProviderRole.EXECUTOR,
            diagnostics=diagnostics.append,
        )
        with TemporaryDirectory(prefix="crewplane-open-pipe-") as temp_dir:
            child_pid_path = Path(temp_dir) / "child.pid"
            child_script = (
                "import os, sys, time\n"
                "os.setsid()\n"
                "open(sys.argv[1], 'w', encoding='utf-8').write(str(os.getpid()))\n"
                "time.sleep(30)\n"
            )
            leader_script = (
                "import subprocess, sys, time\n"
                "path = sys.argv[1]\n"
                "subprocess.Popen([sys.executable, '-c', sys.argv[2], path])\n"
                "while True:\n"
                "    try:\n"
                "        open(path, encoding='utf-8').read()\n"
                "        break\n"
                "    except FileNotFoundError:\n"
                "        time.sleep(0.01)\n"
                "print('leader exited')\n"
            )
            child_pid: int | None = None
            try:
                with pytest.raises(ProcessDrainError, match="pipes remained open"):
                    await asyncio.wait_for(
                        run_command_once(
                            cmd=[
                                sys.executable,
                                "-c",
                                leader_script,
                                child_pid_path.as_posix(),
                                child_script,
                            ],
                            stdin_data=None,
                            log_file=None,
                            append_log=False,
                            log_header=None,
                            cwd=Path.cwd(),
                            invocation_context=context,
                            idle_timeout_seconds=None,
                        ),
                        timeout=3.0,
                    )
                child_pid = int(child_pid_path.read_text(encoding="utf-8"))
                os.kill(child_pid, 0)
                assert any(
                    diagnostic.operation == "process_pipe_drain_timeout"
                    for diagnostic in diagnostics
                )
            finally:
                if child_pid is None and child_pid_path.is_file():
                    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
                if child_pid is not None:
                    with suppress(ProcessLookupError):
                        os.killpg(child_pid, signal.SIGKILL)
                    await asyncio.sleep(0.05)
