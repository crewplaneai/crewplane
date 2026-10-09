import asyncio
import sys
from unittest.mock import patch

import pytest

from crewplane.architecture.contracts import InvocationContext
from crewplane.runtime.agent.invocation.command import run_command_once
from crewplane.runtime.agent.process.windows_launch import WindowsLaunch

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Requires native Proactor subprocesses and Job Objects",
)


async def run_python(script, root, stdin=None, context=None, idle=None):
    return await run_command_once(
        [sys.executable, "-c", script], stdin, None, False, None, root, context, idle
    )


@pytest.mark.parametrize("exit_code", [0, 7])
def test_native_stdin_streams_and_receipts(tmp_path, exit_code) -> None:
    payload = b"before\x1a\r\n" + "日本語".encode() * 200000
    events = []
    context = InvocationContext(
        node_id="node",
        task_id="task",
        provider="generic",
        role="executor",
        process_event_sink=events.append,
    )
    script = f"import os,sys; data=sys.stdin.buffer.read(); os.write(1,data); os.write(2,b'e'*200000); sys.exit({exit_code})"
    result = asyncio.run(run_python(script, tmp_path, payload, context))
    try:
        assert result.returncode == exit_code
        assert result.stdout_path.read_bytes() == payload
        assert result.stderr_path.read_bytes() == b"e" * 200000
        assert [event.status for event in events] == ["started", "exited"]
        assert all(event.process_group_id is None for event in events)
    finally:
        result.stdout_path.unlink()
        result.stderr_path.unlink()


def test_gate_eof_prevents_provider_start(tmp_path) -> None:
    async def check():
        launch = WindowsLaunch()
        try:
            process = await launch.start(
                [sys.executable, "-c", "open('unexpected','w').close()"], tmp_path, None
            )
            process.stdin.close()
            await asyncio.wait_for(process.communicate(), 5)
            assert process.returncode == 125
            await launch.drain()
            assert launch.job.active_process_count() == 0
        finally:
            launch.close()

    asyncio.run(check())
    assert not (tmp_path / "unexpected").exists()


@pytest.mark.parametrize("reason", ["assignment failed", "incompatible enclosing job"])
def test_assignment_failure_never_releases_gate(tmp_path, reason) -> None:
    with (
        patch(
            "crewplane.runtime.agent.process.windows_job.WindowsJob.assign",
            side_effect=OSError(reason),
        ),
        pytest.raises(RuntimeError, match=reason),
    ):
        asyncio.run(run_python("open('unexpected','w').close()", tmp_path))
    assert not (tmp_path / "unexpected").exists()


@pytest.mark.parametrize("inherit_pipes", [True, False])
def test_job_drains_descendants_after_leader_exits(tmp_path, inherit_pipes) -> None:
    child = "import time,pathlib;time.sleep(1);pathlib.Path('escaped').write_text('bad');time.sleep(30)"
    redirects = (
        "" if inherit_pipes else ",stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL"
    )
    script = f"import subprocess,sys;subprocess.Popen([sys.executable,'-c',{child!r}]{redirects});print('leader done')"

    async def check():
        result = await asyncio.wait_for(run_python(script, tmp_path), 8)
        result.stdout_path.unlink()
        result.stderr_path.unlink()
        await asyncio.sleep(1.2)

    asyncio.run(check())
    assert not (tmp_path / "escaped").exists()


def test_cancellation_and_idle_timeout_confirm_tree_cleanup(tmp_path) -> None:
    async def check():
        events = []
        context = InvocationContext(
            node_id="node",
            task_id="task",
            provider="generic",
            role="executor",
            process_event_sink=events.append,
        )
        task = asyncio.create_task(
            run_python("import time;time.sleep(30)", tmp_path, context=context)
        )
        while not events:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert [event.status for event in events] == ["started", "exited"]
        with pytest.raises(RuntimeError, match="no output"):
            await run_python("import time;time.sleep(30)", tmp_path, idle=0.05)

    asyncio.run(check())


def test_missing_native_executable_produces_confirmed_failure(tmp_path) -> None:
    result = asyncio.run(
        run_command_once(
            [str(tmp_path / "missing.exe")],
            None,
            None,
            False,
            None,
            tmp_path,
            None,
            None,
        )
    )
    assert result.returncode == 127
    assert b"Provider launch failed" in result.stderr_path.read_bytes()
    result.stdout_path.unlink()
    result.stderr_path.unlink()


def test_wall_timeout_finishes_cleanup_before_next_attempt(tmp_path):
    async def check():
        events = []
        context = InvocationContext(
            node_id="node",
            task_id="task",
            provider="generic",
            role="executor",
            process_event_sink=events.append,
        )
        task = asyncio.create_task(
            run_python("import time;time.sleep(30)", tmp_path, context=context)
        )
        async with asyncio.timeout(5):
            while not events:
                await asyncio.sleep(0.01)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(task, timeout=0.2)
        assert [event.status for event in events] == ["started", "exited"]
        result = await run_python("print('next attempt')", tmp_path, context=context)
        assert [event.status for event in events] == [
            "started",
            "exited",
            "started",
            "exited",
        ]
        result.stdout_path.unlink()
        result.stderr_path.unlink()

    asyncio.run(check())


@pytest.mark.parametrize("failed_status", ["started", "exited"])
def test_receipt_failure_withholds_execution_or_retains_cleanup_error(
    tmp_path, failed_status
):
    from crewplane.runtime.agent.process.drain import unconfirmed_process_cleanup

    def receipt(event):
        if event.status == failed_status:
            raise OSError("receipt publication unavailable")

    context = InvocationContext(
        node_id="node",
        task_id="task",
        provider="generic",
        role="executor",
        process_event_sink=receipt,
    )
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(
            run_python(
                "open('provider-started','w').close()", tmp_path, context=context
            )
        )
    if failed_status == "started":
        assert not (tmp_path / "provider-started").exists()
    else:
        assert unconfirmed_process_cleanup(failure.value) is not None


def test_native_console_ctrl_c_drains_before_exit_receipt(tmp_path):
    import json
    import subprocess
    from pathlib import Path

    fixture = Path(__file__).parents[3] / "helpers/windows_console_cancel.py"
    process = subprocess.Popen(
        [sys.executable, str(fixture), str(tmp_path)],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        output, errors = process.communicate(timeout=15)
        assert process.returncode == 0, (output, errors)
        assert json.loads((tmp_path / "console-events.json").read_bytes()) == [
            "started",
            "exited",
        ]
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
