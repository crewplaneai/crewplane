from __future__ import annotations

import asyncio
import signal
import subprocess
from typing import cast

import pytest

from crewplane.runtime.agent.process import drain as process_drain
from crewplane.runtime.agent.process.drain import (
    ProcessDrainError,
    drain_async_process,
    drain_popen_process,
    process_group_is_alive,
)


class _StubbornAsyncProcess:
    pid = 123
    returncode: int | None = None

    def __init__(self) -> None:
        self.terminate_calls = 0
        self.kill_calls = 0

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1


class _StubbornPopenProcess:
    pid = 456

    def __init__(self) -> None:
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1


class _MissingOnTerminatePopenProcess:
    pid = 789

    def __init__(self) -> None:
        self.stopped = False

    def poll(self) -> int | None:
        return -15 if self.stopped else None

    def terminate(self) -> None:
        self.stopped = True
        raise ProcessLookupError

    def kill(self) -> None:
        raise AssertionError("kill should not run after the process stops")


def test_async_process_drain_fails_after_finite_term_and_kill_deadlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _StubbornAsyncProcess()
    monkeypatch.setattr(process_drain, "PROCESS_GROUP_TERM_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(process_drain, "PROCESS_GROUP_KILL_GRACE_SECONDS", 0.0)

    async def run() -> None:
        with pytest.raises(ProcessDrainError) as exc_info:
            await drain_async_process(
                cast(asyncio.subprocess.Process, process),
                None,
            )
        assert exc_info.value.evidence.leader_stopped is False

    asyncio.run(run())

    assert process.terminate_calls == 1
    assert process.kill_calls == 1


def test_popen_process_drain_fails_after_finite_term_and_kill_deadlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _StubbornPopenProcess()
    monkeypatch.setattr(process_drain, "PROCESS_GROUP_TERM_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(process_drain, "PROCESS_GROUP_KILL_GRACE_SECONDS", 0.0)

    with pytest.raises(ProcessDrainError) as exc_info:
        drain_popen_process(cast(subprocess.Popen[str], process), None)

    assert exc_info.value.evidence.leader_stopped is False
    assert process.terminate_calls == 1
    assert process.kill_calls == 1


def test_popen_process_drain_handles_process_disappearing_during_term(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _MissingOnTerminatePopenProcess()
    monkeypatch.setattr(process_drain, "PROCESS_GROUP_TERM_GRACE_SECONDS", 0.0)

    evidence = drain_popen_process(cast(subprocess.Popen[str], process), None)

    assert evidence.leader_stopped is True


def test_popen_process_drain_targets_live_posix_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _StubbornPopenProcess()
    monkeypatch.setattr(process_drain, "PROCESS_GROUP_TERM_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(process_drain, "PROCESS_GROUP_KILL_GRACE_SECONDS", 0.0)

    def accept_signal(
        process_group_id: int,
        signal_number: signal.Signals | int,
    ) -> None:
        del process_group_id, signal_number

    monkeypatch.setattr(process_drain.os, "killpg", accept_signal)

    with pytest.raises(ProcessDrainError):
        drain_popen_process(cast(subprocess.Popen[str], process), 456)

    assert process.terminate_calls == 0
    assert process.kill_calls == 0


def test_popen_process_drain_falls_back_to_leader_after_group_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, signal.Signals]] = []
    process = _StubbornPopenProcess()
    monkeypatch.setattr(process_drain, "PROCESS_GROUP_TERM_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(process_drain, "PROCESS_GROUP_KILL_GRACE_SECONDS", 0.0)

    def group_is_alive(process_group_id: int | None) -> bool:
        assert process_group_id == process.pid
        return False

    monkeypatch.setattr(process_drain, "process_group_is_alive", group_is_alive)
    monkeypatch.setattr(
        process,
        "terminate",
        lambda: events.append(("leader", signal.SIGTERM)),
    )
    monkeypatch.setattr(
        process,
        "kill",
        lambda: events.append(("leader", signal.SIGKILL)),
    )

    def reject_group(
        process_group_id: int,
        signal_number: signal.Signals,
    ) -> bool:
        assert process_group_id == process.pid
        events.append(("group", signal_number))
        return False

    with pytest.raises(ProcessDrainError):
        drain_popen_process(
            cast(subprocess.Popen[str], process),
            process.pid,
            reject_group,
        )

    assert events == [
        ("group", signal.SIGTERM),
        ("leader", signal.SIGTERM),
        ("group", signal.SIGKILL),
        ("leader", signal.SIGKILL),
    ]


def test_process_group_permission_error_is_treated_as_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny_signal(process_group_id: int, signal_number: int) -> None:
        del process_group_id, signal_number
        raise PermissionError

    monkeypatch.setattr(process_drain.os, "killpg", deny_signal)

    assert process_group_is_alive(123) is True


def test_async_process_drain_reports_permission_denied_group_as_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _StubbornAsyncProcess()
    monkeypatch.setattr(process_drain, "PROCESS_GROUP_TERM_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(process_drain, "PROCESS_GROUP_KILL_GRACE_SECONDS", 0.0)

    def deny_signal(process_group_id: int, signal_number: int) -> None:
        del process_group_id, signal_number
        raise PermissionError

    monkeypatch.setattr(process_drain.os, "killpg", deny_signal)

    async def run() -> None:
        with pytest.raises(ProcessDrainError) as exc_info:
            await drain_async_process(
                cast(asyncio.subprocess.Process, process),
                123,
            )
        assert exc_info.value.evidence.process_group_stopped is False

    asyncio.run(run())


def test_async_process_drain_reports_permission_denied_process_as_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _StubbornAsyncProcess()
    monkeypatch.setattr(process_drain, "PROCESS_GROUP_TERM_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(process_drain, "PROCESS_GROUP_KILL_GRACE_SECONDS", 0.0)

    def deny_signal() -> None:
        raise PermissionError

    monkeypatch.setattr(process, "terminate", deny_signal)
    monkeypatch.setattr(process, "kill", deny_signal)

    async def run() -> None:
        with pytest.raises(ProcessDrainError) as exc_info:
            await drain_async_process(
                cast(asyncio.subprocess.Process, process),
                None,
            )
        assert exc_info.value.evidence.leader_stopped is False

    asyncio.run(run())


def test_popen_process_drain_reports_permission_denied_signaller_as_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _StubbornPopenProcess()
    monkeypatch.setattr(process_drain, "PROCESS_GROUP_TERM_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(process_drain, "PROCESS_GROUP_KILL_GRACE_SECONDS", 0.0)

    def deny_signal(
        process_group_id: int,
        signal_number: signal.Signals,
    ) -> bool:
        del process_group_id, signal_number
        raise PermissionError

    monkeypatch.setattr(process_drain.os, "killpg", deny_signal)

    with pytest.raises(ProcessDrainError) as exc_info:
        drain_popen_process(
            cast(subprocess.Popen[str], process),
            456,
            deny_signal,
        )

    assert exc_info.value.evidence.process_group_stopped is False


def test_popen_process_drain_reports_permission_denied_process_as_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _StubbornPopenProcess()
    monkeypatch.setattr(process_drain, "PROCESS_GROUP_TERM_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(process_drain, "PROCESS_GROUP_KILL_GRACE_SECONDS", 0.0)

    def deny_signal() -> None:
        raise PermissionError

    monkeypatch.setattr(process, "terminate", deny_signal)
    monkeypatch.setattr(process, "kill", deny_signal)

    with pytest.raises(ProcessDrainError) as exc_info:
        drain_popen_process(cast(subprocess.Popen[str], process), None)

    assert exc_info.value.evidence.leader_stopped is False


def test_successful_process_group_probe_is_treated_as_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def accept_signal(process_group_id: int, signal_number: int) -> None:
        del process_group_id, signal_number

    monkeypatch.setattr(process_drain.os, "killpg", accept_signal)

    assert process_group_is_alive(123) is True


def test_missing_process_group_is_treated_as_drained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _StubbornAsyncProcess()
    monkeypatch.setattr(process_drain, "PROCESS_GROUP_TERM_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(process_drain, "PROCESS_GROUP_KILL_GRACE_SECONDS", 0.0)

    def missing_group(
        process_group_id: int,
        signal_number: signal.Signals | int,
    ) -> None:
        del process_group_id, signal_number
        raise ProcessLookupError

    monkeypatch.setattr(process_drain.os, "killpg", missing_group)

    evidence = asyncio.run(
        drain_async_process(
            cast(asyncio.subprocess.Process, process),
            123,
        )
    )

    assert evidence.leader_stopped is True
    assert evidence.process_group_stopped is True
