"""Run one foreground tool while inheriting the outer provider's ownership."""

from __future__ import annotations

import asyncio
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .limits import COMMAND_DRAIN_GRACE_SECONDS, MAX_COMMAND_OUTPUT_BYTES


@dataclass
class CommandInput:
    argv: list[str]
    stdin: bytes


def parse_arguments(arguments: dict[str, object]) -> CommandInput:
    if arguments.keys() - {"argv", "stdin"}:
        raise ValueError("run_command accepts only argv and optional stdin")
    argv = arguments.get("argv")
    if not isinstance(argv, list) or not argv:
        raise ValueError("run_command argv must be a nonempty array of strings")
    if any(not isinstance(value, str) or "\0" in value for value in argv):
        raise ValueError("run_command argv must contain strings without NUL bytes")
    executable = argv[0]
    if not executable.strip():
        raise ValueError("run_command requires a nonempty executable")
    stdin = arguments.get("stdin", "")
    if not isinstance(stdin, str) or "\0" in stdin:
        raise ValueError("run_command stdin must be a string without NUL bytes")
    try:
        data = stdin.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("run_command stdin must be valid UTF-8 text") from exc
    reject_windows_batch(executable)
    return CommandInput(argv, data)


def reject_windows_batch(executable: str) -> None:
    if platform.system() != "Windows":
        return
    resolved = shutil.which(executable)
    candidates = [executable] if resolved is None else [executable, resolved]
    if any(
        Path(value).resolve().suffix.lower() in {".bat", ".cmd"} for value in candidates
    ):
        raise ValueError(
            "Direct Windows batch-file execution is unsupported; use a native executable or explicit interpreter"
        )


@dataclass
class StreamCapture:
    total_bytes: int = 0
    head: bytearray = field(default_factory=bytearray)
    tail: bytearray = field(default_factory=bytearray)

    def append(self, data: bytes) -> None:
        self.total_bytes += len(data)
        half = MAX_COMMAND_OUTPUT_BYTES // 2
        head_count = min(len(data), half - len(self.head))
        self.head.extend(data[:head_count])
        remaining = len(data) - head_count
        if remaining >= half:
            self.tail[:] = data[-half:]
        else:
            overflow = max(0, len(self.tail) + remaining - half)
            del self.tail[:overflow]
            self.tail.extend(data[head_count:])

    def text(self) -> str:
        return (self.head + self.tail).decode("utf-8", errors="replace")

    @property
    def truncated(self) -> bool:
        return self.total_bytes > MAX_COMMAND_OUTPUT_BYTES


class CommandProtocol(asyncio.SubprocessProtocol):
    """Observe direct exit independently of EOF on inherited descendant pipes."""

    def __init__(self) -> None:
        loop = asyncio.get_running_loop()
        self.exited: asyncio.Future[None] = loop.create_future()
        self.pipes_closed: asyncio.Future[None] = loop.create_future()
        self.write_ready = asyncio.Event()
        self.write_ready.set()
        self.closed_pipes: set[int] = set()
        self.output = {1: StreamCapture(), 2: StreamCapture()}
        self.pipe_errors: list[str] = []

    def pipe_data_received(self, fd: int, data: bytes) -> None:
        self.output[fd].append(data)

    def pipe_connection_lost(self, fd: int, exc: Exception | None) -> None:
        self.closed_pipes.add(fd)
        if exc is not None and not (
            fd == 0 and isinstance(exc, (BrokenPipeError, ConnectionResetError))
        ):
            self.pipe_errors.append(f"pipe {fd}: {type(exc).__name__}: {exc}")
        if fd == 0:
            self.write_ready.set()
        if self.closed_pipes == {0, 1, 2} and not self.pipes_closed.done():
            self.pipes_closed.set_result(None)

    def process_exited(self) -> None:
        if not self.exited.done():
            self.exited.set_result(None)

    def pause_writing(self) -> None:
        self.write_ready.clear()

    def resume_writing(self) -> None:
        self.write_ready.set()

    def result(self, exit_code: int) -> dict[str, object]:
        stdout, stderr = self.output[1], self.output[2]
        return {
            "exit_code": exit_code,
            "stdout": stdout.text(),
            "stderr": stderr.text(),
            "stdout_bytes": stdout.total_bytes,
            "stderr_bytes": stderr.total_bytes,
            "stdout_truncated": stdout.truncated,
            "stderr_truncated": stderr.truncated,
        }


async def feed_stdin(
    transport: asyncio.SubprocessTransport, protocol: CommandProtocol, data: bytes
) -> None:
    writer = transport.get_pipe_transport(0)
    assert isinstance(writer, asyncio.WriteTransport)
    try:
        for offset in range(0, len(data), 65536):
            await protocol.write_ready.wait()
            if 0 in protocol.closed_pipes:
                break
            writer.write(data[offset : offset + 65536])
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        writer.close()


async def collect_command(
    transport: asyncio.SubprocessTransport, protocol: CommandProtocol
) -> dict[str, object]:
    await asyncio.shield(protocol.exited)
    try:
        await asyncio.wait_for(
            asyncio.shield(protocol.pipes_closed), COMMAND_DRAIN_GRACE_SECONDS
        )
    except TimeoutError as exc:
        raise ValueError(
            "Command exited but inherited pipes remained open; command cleanup is uncertain"
        ) from exc
    if protocol.pipe_errors:
        raise ValueError("Command I/O failed: " + "; ".join(protocol.pipe_errors))
    exit_code = transport.get_returncode()
    assert exit_code is not None
    return protocol.result(exit_code)


async def run_command(command: CommandInput) -> dict[str, object]:
    loop = asyncio.get_running_loop()
    try:
        transport, protocol = await loop.subprocess_exec(
            CommandProtocol,
            *command.argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
    except (OSError, UnicodeError) as exc:
        return {
            "error": f"Command could not launch ({type(exc).__name__}): {exc}",
            "exit_code": None,
        }
    feeder = asyncio.create_task(feed_stdin(transport, protocol, command.stdin))
    try:
        result = await collect_command(transport, protocol)
        await feeder
        return result
    finally:
        feeder.cancel()
        await asyncio.gather(feeder, return_exceptions=True)
        transport.close()
        try:
            await asyncio.wait_for(
                asyncio.shield(protocol.exited), COMMAND_DRAIN_GRACE_SECONDS
            )
        except TimeoutError as exc:
            raise ValueError("Command child cleanup could not be confirmed") from exc


async def execute_command(arguments: dict[str, object]) -> dict[str, object]:
    try:
        command = parse_arguments(arguments)
    except ValueError as exc:
        return {"error": str(exc), "exit_code": None}
    return await run_command(command)
