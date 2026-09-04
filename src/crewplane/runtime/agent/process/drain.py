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
_PROVIDER_PROCESS_DRAIN_FAILURE = (
    "Provider process group could not be drained within the finite TERM/KILL deadline."
)
_WORKSPACE_SETUP_PROCESS_DRAIN_FAILURE = (
    "Workspace setup process group could not be drained within the finite "
    "TERM/KILL deadline."
)


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


@dataclass(frozen=True, slots=True)
class _DrainPhase:
    signal_number: signal.Signals
    grace_seconds: float


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
    leader_was_missing = False
    for phase in _drain_phases():
        if _async_process_is_drained(process, process_group_id):
            break
        leader_was_missing = (
            await _run_async_drain_phase(process, process_group_id, phase)
            or leader_was_missing
        )
    evidence = _process_drain_evidence(
        process.pid,
        process_group_id,
        process.returncode is not None or leader_was_missing,
    )
    return _require_complete_drain(evidence, _PROVIDER_PROCESS_DRAIN_FAILURE)


def drain_popen_process(
    process: subprocess.Popen[str],
    process_group_id: int | None,
    group_signaller: Callable[[int, signal.Signals], bool] | None = None,
) -> ProcessDrainEvidence:
    for phase in _drain_phases():
        if _popen_process_is_drained(process, process_group_id):
            break
        _run_popen_drain_phase(
            process,
            process_group_id,
            group_signaller,
            phase,
        )
    evidence = _process_drain_evidence(
        process.pid,
        process_group_id,
        process.poll() is not None,
    )
    return _require_complete_drain(
        evidence,
        _WORKSPACE_SETUP_PROCESS_DRAIN_FAILURE,
    )


def _drain_phases() -> tuple[_DrainPhase, ...]:
    return (
        _DrainPhase(signal.SIGTERM, PROCESS_GROUP_TERM_GRACE_SECONDS),
        _DrainPhase(signal.SIGKILL, PROCESS_GROUP_KILL_GRACE_SECONDS),
    )


async def _run_async_drain_phase(
    process: asyncio.subprocess.Process,
    process_group_id: int | None,
    phase: _DrainPhase,
) -> bool:
    leader_was_missing = _signal_async_process(
        process,
        process_group_id,
        phase.signal_number,
    )
    await _wait_for_async_drain(
        process,
        process_group_id,
        phase.grace_seconds,
    )
    return leader_was_missing


def _run_popen_drain_phase(
    process: subprocess.Popen[str],
    process_group_id: int | None,
    group_signaller: Callable[[int, signal.Signals], bool] | None,
    phase: _DrainPhase,
) -> None:
    _signal_popen_process(
        process,
        process_group_id,
        phase.signal_number,
        group_signaller,
    )
    _wait_for_popen_drain(
        process,
        process_group_id,
        phase.grace_seconds,
    )


def _process_drain_evidence(
    pid: int,
    process_group_id: int | None,
    leader_stopped: bool,
) -> ProcessDrainEvidence:
    return ProcessDrainEvidence(
        pid=pid,
        process_group_id=process_group_id,
        leader_stopped=leader_stopped,
        process_group_stopped=not process_group_is_alive(process_group_id),
    )


def _require_complete_drain(
    evidence: ProcessDrainEvidence,
    failure_reason: str,
) -> ProcessDrainEvidence:
    if evidence.leader_stopped and evidence.process_group_stopped:
        return evidence
    raise ProcessDrainError(evidence, failure_reason)


def _async_process_is_drained(
    process: asyncio.subprocess.Process,
    process_group_id: int | None,
) -> bool:
    return process.returncode is not None and not process_group_is_alive(
        process_group_id
    )


def _popen_process_is_drained(
    process: subprocess.Popen[str],
    process_group_id: int | None,
) -> bool:
    return process.poll() is not None and not process_group_is_alive(process_group_id)


async def _wait_for_async_drain(
    process: asyncio.subprocess.Process,
    process_group_id: int | None,
    timeout_seconds: float,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if _async_process_is_drained(process, process_group_id):
            return
        await asyncio.sleep(PROCESS_DRAIN_POLL_SECONDS)


def _wait_for_popen_drain(
    process: subprocess.Popen[str],
    process_group_id: int | None,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _popen_process_is_drained(process, process_group_id):
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
            process.kill()
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
    if _signal_popen_group(process_group_id, signal_number, group_signaller):
        return
    _signal_popen_leader(process, signal_number)


def _signal_popen_group(
    process_group_id: int | None,
    signal_number: signal.Signals,
    group_signaller: Callable[[int, signal.Signals], bool] | None,
) -> bool:
    if process_group_id is None or group_signaller is None:
        group_targeted, _group_missing = _signal_group(
            process_group_id,
            signal_number,
        )
        return group_targeted
    try:
        return group_signaller(process_group_id, signal_number)
    except PermissionError:
        return True


def _signal_popen_leader(
    process: subprocess.Popen[str],
    signal_number: signal.Signals,
) -> None:
    try:
        if signal_number == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
    except (PermissionError, ProcessLookupError):
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
