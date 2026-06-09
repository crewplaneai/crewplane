from __future__ import annotations

import asyncio
import os
import signal
from asyncio import sleep as asyncio_sleep

from orchestrator_cli.architecture.contracts import InvocationDiagnosticSink

from .diagnostics import emit_process_already_exited_diagnostic

PROCESS_EXIT_POLL_INTERVAL_SECONDS = 0.01
PROCESS_GROUP_TERMINATE_GRACE_SECONDS = 0.05


async def wait_for_process_exit(process: asyncio.subprocess.Process) -> int:
    while process.returncode is None:
        await asyncio_sleep(PROCESS_EXIT_POLL_INTERVAL_SECONDS)
    return process.returncode


def send_process_group_signal(
    process_group_id: int | None,
    signal_number: int,
) -> bool:
    if process_group_id is None or os.name != "posix":
        return False
    try:
        os.killpg(process_group_id, signal_number)
    except (PermissionError, ProcessLookupError):
        return False
    return True


def terminate_process_or_group(
    process: asyncio.subprocess.Process,
    process_group_id: int | None,
) -> None:
    if send_process_group_signal(process_group_id, signal.SIGTERM):
        return
    process.terminate()


def kill_process_or_group(
    process: asyncio.subprocess.Process,
    process_group_id: int | None,
) -> None:
    if send_process_group_signal(process_group_id, signal.SIGKILL):
        return
    process.kill()


async def terminate_process_group(process_group_id: int | None) -> None:
    if not send_process_group_signal(process_group_id, signal.SIGTERM):
        return
    await asyncio_sleep(PROCESS_GROUP_TERMINATE_GRACE_SECONDS)
    send_process_group_signal(process_group_id, signal.SIGKILL)


async def reap_failed_process(
    process: asyncio.subprocess.Process,
    process_group_id: int | None = None,
    diagnostic_sink: InvocationDiagnosticSink | None = None,
) -> None:
    if process.returncode is not None:
        await terminate_process_group(process_group_id)
        return
    try:
        terminate_process_or_group(process, process_group_id)
    except ProcessLookupError:
        emit_process_already_exited_diagnostic(diagnostic_sink, "terminate")
        return

    try:
        await asyncio.wait_for(wait_for_process_exit(process), timeout=1.0)
    except TimeoutError:
        if process.returncode is None:
            try:
                kill_process_or_group(process, process_group_id)
            except ProcessLookupError:
                emit_process_already_exited_diagnostic(diagnostic_sink, "kill")
                return
        await wait_for_process_exit(process)
    await terminate_process_group(process_group_id)
