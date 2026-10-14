import asyncio
import signal
import sys
from pathlib import Path

from crewplane.runtime.agent.invocation.command import run_command_once


def test_block() -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    command = [
        sys.executable,
        str(Path(__file__).with_name("sleeping_process.py")),
        "--pid-file",
        "provider.pid",
    ]
    asyncio.run(
        run_command_once(command, None, None, False, None, Path.cwd(), None, None)
    )
