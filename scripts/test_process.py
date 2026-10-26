"""Bounded ownership of a test command and its pytest workers."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37


@dataclass(frozen=True)
class ProcessOutcome:
    returncode: int
    seconds: float
    interrupted: bool = False
    timed_out: bool = False
    forced: bool = False


def launch_command(command: list[str]) -> list[str]:
    if os.name == "nt":
        # A venv redirector can spawn the gate before its Job assignment.
        gate = Path(__file__).with_name("test_process_gate.py")
        return [sys._base_executable, str(gate), *command]
    return command


@contextmanager
def shutdown_signals() -> Iterator[list[int]]:
    requested: list[int] = []

    def request(signum: int, frame: object) -> None:
        del frame
        requested.append(signum)

    signals = [signal.SIGINT, signal.SIGTERM]
    if os.name == "nt":
        signals.append(signal.SIGBREAK)
    previous = {number: signal.signal(number, request) for number in signals}
    try:
        yield requested
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


@contextmanager
def child_subreaper() -> Iterator[bool]:
    """Adopt orphans while the runner exclusively owns its child processes."""
    if sys.platform != "linux":
        yield False
        return
    prctl = ctypes.CDLL(None, use_errno=True).prctl
    prctl.argtypes = [ctypes.c_int, *([ctypes.c_ulong] * 4)]
    prctl.restype = ctypes.c_int

    def configure(option: int, value: int) -> None:
        if prctl(option, value, 0, 0, 0) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))

    previous = ctypes.c_int()
    configure(PR_GET_CHILD_SUBREAPER, ctypes.addressof(previous))
    configure(PR_SET_CHILD_SUBREAPER, 1)
    try:
        # Keep detached providers owned even if their pytest worker crashes.
        yield True
    finally:
        configure(PR_SET_CHILD_SUBREAPER, previous.value)


@dataclass(frozen=True)
class PosixProcessIdentity:
    parent_pid: int
    started: str


def posix_processes() -> dict[int, PosixProcessIdentity]:
    output = subprocess.check_output(
        ["ps", "-axo", "pid=,ppid=,lstart="],
        text=True,
        env={**os.environ, "LC_ALL": "C"},
        timeout=1,
    )
    processes = {}
    for line in output.splitlines():
        pid, parent_pid, started = line.split(maxsplit=2)
        processes[int(pid)] = PosixProcessIdentity(int(parent_pid), started)
    return processes


class OwnedProcess:
    def __init__(
        self, process: subprocess.Popen[bytes], subreaper: bool = False
    ) -> None:
        self.process = process
        self.subreaper = subreaper
        self.descendants: dict[int, str] = {}
        self.job = None
        if os.name == "nt":
            # Reuse the existing native ownership primitive; the gate prevents
            # pytest from launching workers before assignment succeeds.
            from crewplane.runtime.agent.process.windows_job import WindowsJob

            self.job = WindowsJob()
            try:
                self.job.assign(process.pid)
                assert process.stdin is not None
                process.stdin.write(b"1")
                process.stdin.close()
            except BaseException:
                self.job.close()
                raise

    def signal_shutdown(self) -> None:
        if os.name == "nt":
            if self.process.poll() is None:
                self.process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            # Preserve ancestry before pytest exits and providers are reparented.
            processes = posix_processes()
            parents = {self.process.pid}
            while parents:
                children = {
                    pid
                    for pid, identity in processes.items()
                    if identity.parent_pid in parents
                }
                self.descendants.update(
                    (pid, processes[pid].started) for pid in children
                )
                parents = children
            self.signal_group(signal.SIGINT)

    def signal_group(self, number: int) -> None:
        with suppress(ProcessLookupError):
            os.killpg(self.process.pid, number)

    def surviving_descendants(self) -> list[int]:
        if not self.descendants:
            return []
        processes = posix_processes()
        return [
            pid
            for pid, started in self.descendants.items()
            if pid in processes and processes[pid].started == started
        ]

    def reap_exited_children(self) -> None:
        if not self.subreaper:
            return
        while True:
            try:
                child = os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                return
            if child is None:
                return
            if child.si_pid == self.process.pid:
                self.process.poll()
            else:
                os.waitpid(child.si_pid, 0)

    def adopted_children(self) -> list[int]:
        if not self.subreaper:
            return []
        children = []
        for pid, identity in posix_processes().items():
            if identity.parent_pid != os.getpid() or pid == self.process.pid:
                continue
            with suppress(ChildProcessError):
                if os.waitpid(pid, os.WNOHANG)[0] == 0:
                    children.append(pid)
        return children

    def has_children(self) -> bool:
        if self.job is not None:
            return self.job.active_process_count() > 0
        if self.subreaper:
            if self.process.poll() is None:
                return True
            # Only ECHILD proves cleanup is complete; snapshots can miss orphans.
            while True:
                try:
                    if os.waitpid(-1, os.WNOHANG)[0] == 0:
                        return True
                except ChildProcessError:
                    return False
        try:
            os.killpg(self.process.pid, 0)
        except ProcessLookupError:
            return bool(self.surviving_descendants())
        return True

    def kill(self) -> None:
        if self.job is not None:
            self.job.terminate()
        else:
            try:
                for pid in self.adopted_children() + self.surviving_descendants():
                    with suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGKILL)
            finally:
                self.signal_group(signal.SIGKILL)

    def close(self, timeout: float) -> None:
        try:
            if self.has_children():
                self.kill()
            self.process.wait(timeout=timeout)
            if self.job is not None or self.subreaper:
                deadline = time.monotonic() + timeout
                while self.has_children():
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Owned test processes did not exit")
                    if self.subreaper:
                        self.kill()
                    time.sleep(0.05)
        finally:
            if self.job is not None:
                self.job.close()


def run_logged(
    command: list[str],
    cwd: Path,
    environment: dict[str, str],
    log: Path,
    timeout: float | None = None,
    shutdown_timeout: float = 5,
) -> ProcessOutcome:
    started = time.monotonic()
    reap_timeout = max(shutdown_timeout, 5.0)
    forced = False
    timed_out = False
    with (
        log.open("wb") as output,
        log.open("rb") as reader,
        shutdown_signals() as requested,
        child_subreaper() as subreaper,
    ):
        process = subprocess.Popen(
            launch_command(command),
            cwd=cwd,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if os.name == "nt" else subprocess.DEVNULL,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        owned = None
        receipt = log.with_suffix(".active-pid")
        try:
            owned = OwnedProcess(process, subreaper)
            receipt.write_text(str(process.pid), encoding="utf-8")
            deadline = None
            while process.poll() is None:
                owned.reap_exited_children()
                _display_output(reader)
                now = time.monotonic()
                timed_out = timed_out or (
                    timeout is not None and now - started >= timeout
                )
                if deadline is None and (requested or timed_out):
                    owned.signal_shutdown()
                    deadline = now + shutdown_timeout
                if deadline is not None and (now >= deadline or len(requested) > 1):
                    owned.kill()
                    forced = True
                    process.wait(timeout=reap_timeout)
                    break
                time.sleep(0.05)
            returncode = process.wait(timeout=reap_timeout)
            if owned.has_children():
                forced = True
            _display_output(reader)
        finally:
            if owned is not None:
                owned.close(reap_timeout)
            else:
                process.kill()
                process.wait(timeout=reap_timeout)
                if process.stdin is not None:
                    process.stdin.close()
            receipt.unlink(missing_ok=True)
    return ProcessOutcome(
        returncode=returncode
        if not (requested or timed_out or forced)
        else returncode or 1,
        seconds=time.monotonic() - started,
        interrupted=bool(requested),
        timed_out=timed_out,
        forced=forced,
    )


def _display_output(reader) -> None:
    payload = reader.read()
    if payload:
        display_text(payload.decode("utf-8", errors="replace"))


def display_text(text: str) -> None:
    encoding = sys.stdout.encoding or "utf-8"
    display = text.encode(encoding, errors="backslashreplace").decode(encoding)
    sys.stdout.write(display)
    sys.stdout.flush()
