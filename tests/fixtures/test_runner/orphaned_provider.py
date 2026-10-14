import asyncio
import os
import sys
from pathlib import Path

from crewplane.runtime.agent.invocation.command import run_command_once


async def crash_after_child_starts() -> None:
    command = [
        sys.executable,
        str(Path(__file__).with_name("sleeping_process.py")),
        "--pid-file",
        "provider.pid",
        "--child-pid-file",
        "child.pid",
    ]
    task = asyncio.create_task(
        run_command_once(command, None, None, False, None, Path.cwd(), None, None)
    )
    async with asyncio.timeout(10):
        while not all(
            Path(name).exists() and Path(name).read_text().isdigit()
            for name in ("provider.pid", "child.pid")
        ):
            if task.done():
                await task
                raise AssertionError("provider exited before readiness")
            await asyncio.sleep(0.005)
    os._exit(7)


def test_crash() -> None:
    asyncio.run(crash_after_child_starts())
