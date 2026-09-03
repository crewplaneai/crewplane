from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass

PROCESS_GROUP_TERM_GRACE_SECONDS = 0.25
PROCESS_GROUP_KILL_GRACE_SECONDS = 1.0
PROCESS_DRAIN_POLL_SECONDS = 0.01


@dataclass(frozen=True)
class ProcessDrainEvidence:
    pid: int
    process_group_id: int | None
    leader_stopped: bool
    process_group_stopped: bool


class ProcessDrainError(RuntimeError):
    def __init__(self, evidence: ProcessDrainEvidence, reason: str) -> None:
        super().__init__(reason)
        self.evidence = evidence


def process_group_is_alive(process_group_id: int | None) -> bool:
    if process_group_id is None or os.name != "posix":
        return False
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def drain_async_process(
    process: asyncio.subprocess.Process,
    process_group_id: int | None,
) -> ProcessDrainEvidence:
    process_missing = False
    if process.returncode is None or process_group_is_alive(process_group_id):
        process_missing = _signal_async_process(
            process,
            process_group_id,
            signal.SIGTERM,
        )
        await _wait_for_async_drain(
            process,
            process_group_id,
            PROCESS_GROUP_TERM_GRACE_SECONDS,
        )
    if process.returncode is None or process_group_is_alive(process_group_id):
        process_missing = (
            _signal_async_process(
                process,
                process_group_id,
                signal.SIGKILL,
            )
            or process_missing
        )
        await _wait_for_async_drain(
            process,
            process_group_id,
            PROCESS_GROUP_KILL_GRACE_SECONDS,
        )
    evidence = ProcessDrainEvidence(
        pid=getattr(process, "pid", -1),
        process_group_id=process_group_id,
        leader_stopped=process.returncode is not None or process_missing,
        process_group_stopped=not process_group_is_alive(process_group_id),
    )
    if not evidence.leader_stopped or not evidence.process_group_stopped:
        raise ProcessDrainError(
            evidence,
            "Provider process group could not be drained within the finite "
            "TERM/KILL deadline.",
        )
    return evidence


def drain_popen_process(
    process: subprocess.Popen[str],
    process_group_id: int | None,
    group_signaller: Callable[[int, signal.Signals], bool] | None = None,
) -> ProcessDrainEvidence:
    if process.poll() is None or process_group_is_alive(process_group_id):
        _signal_popen_process(
            process,
            process_group_id,
            signal.SIGTERM,
            group_signaller,
        )
        _wait_for_popen_drain(
            process,
            process_group_id,
            PROCESS_GROUP_TERM_GRACE_SECONDS,
        )
    if process.poll() is None or process_group_is_alive(process_group_id):
        _signal_popen_process(
            process,
            process_group_id,
            signal.SIGKILL,
            group_signaller,
        )
        _wait_for_popen_drain(
            process,
            process_group_id,
            PROCESS_GROUP_KILL_GRACE_SECONDS,
        )
    evidence = ProcessDrainEvidence(
        pid=process.pid,
        process_group_id=process_group_id,
        leader_stopped=process.poll() is not None,
        process_group_stopped=not process_group_is_alive(process_group_id),
    )
    if not evidence.leader_stopped or not evidence.process_group_stopped:
        raise ProcessDrainError(
            evidence,
            "Workspace setup process group could not be drained within the finite "
            "TERM/KILL deadline.",
        )
    return evidence


async def _wait_for_async_drain(
    process: asyncio.subprocess.Process,
    process_group_id: int | None,
    timeout_seconds: float,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if process.returncode is not None and not process_group_is_alive(
            process_group_id
        ):
            return
        await asyncio.sleep(PROCESS_DRAIN_POLL_SECONDS)


def _wait_for_popen_drain(
    process: subprocess.Popen[str],
    process_group_id: int | None,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None and not process_group_is_alive(process_group_id):
            return
        time.sleep(PROCESS_DRAIN_POLL_SECONDS)


def _signal_async_process(
    process: asyncio.subprocess.Process,
    process_group_id: int | None,
    signal_number: signal.Signals,
) -> bool:
    group_targeted, group_missing = _signal_group(process_group_id, signal_number)
    if group_targeted:
        return group_missing
    try:
        if signal_number == signal.SIGTERM:
            process.terminate()
        else:
            kill = getattr(process, "kill", process.terminate)
            kill()
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _signal_popen_process(
    process: subprocess.Popen[str],
    process_group_id: int | None,
    signal_number: signal.Signals,
    group_signaller: Callable[[int, signal.Signals], bool] | None,
) -> None:
    if process_group_id is not None and group_signaller is not None:
        try:
            group_targeted = group_signaller(process_group_id, signal_number)
        except PermissionError:
            return
        if group_targeted:
            return
    else:
        group_targeted, _group_missing = _signal_group(
            process_group_id,
            signal_number,
        )
        if group_targeted:
            return
    try:
        if signal_number == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
    except ProcessLookupError:
        return
    except PermissionError:
        return


def _signal_group(
    process_group_id: int | None,
    signal_number: signal.Signals,
) -> tuple[bool, bool]:
    if process_group_id is None or os.name != "posix":
        return False, False
    try:
        os.killpg(process_group_id, signal_number)
    except ProcessLookupError:
        return True, True
    except PermissionError:
        return True, False
    return True, False
