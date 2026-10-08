"""Native console Ctrl+C fixture, launched in its own console by pytest."""

import asyncio
import ctypes
import json
import sys
from pathlib import Path

from crewplane.architecture.contracts import InvocationContext
from crewplane.runtime.agent.invocation.command import run_command_once


def main():
    root = Path(sys.argv[1])
    events = []
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, ctypes.c_int]
    api.GenerateConsoleCtrlEvent.argtypes = [ctypes.c_uint, ctypes.c_uint]
    if not api.SetConsoleCtrlHandler(None, False):
        raise ctypes.WinError()

    async def exercise():
        context = InvocationContext(
            node_id="node",
            task_id="task",
            provider="generic",
            role="executor",
            process_event_sink=events.append,
        )
        task = asyncio.create_task(
            run_command_once(
                [sys.executable, "-c", "import time;time.sleep(30)"],
                None,
                None,
                False,
                None,
                root,
                context,
                None,
            )
        )
        while not events:
            await asyncio.sleep(0.01)
        if not api.GenerateConsoleCtrlEvent(0, 0):
            raise ctypes.WinError()
        await task

    try:
        asyncio.run(exercise())
    except KeyboardInterrupt:
        (root / "console-events.json").write_bytes(
            json.dumps([event.status for event in events]).encode()
        )
    else:
        raise RuntimeError("Expected console Ctrl+C cancellation")


if __name__ == "__main__":
    main()
