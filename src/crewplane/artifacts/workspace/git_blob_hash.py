from __future__ import annotations

import hashlib
import selectors
import subprocess
from time import monotonic


def git_stdout_sha256(
    command_prefix: list[str],
    env: dict[str, str],
    object_id: str,
    timeout_seconds: float,
) -> str:
    command = [*command_prefix, "cat-file", "blob", object_id]
    digest = hashlib.sha256()
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    stdout = process.stdout
    try:
        if stdout is None:
            raise ValueError("Failed to capture Git blob stdout.")
        deadline = monotonic() + timeout_seconds
        selector = selectors.DefaultSelector()
        try:
            selector.register(stdout, selectors.EVENT_READ)
            while True:
                wait_for_stdout(process, selector, command, deadline, timeout_seconds)
                chunk = stdout.read1(1024 * 1024)
                if chunk:
                    digest.update(chunk)
                    continue
                return_code = wait_for_exit(
                    process,
                    command,
                    deadline,
                    timeout_seconds,
                )
                break
        finally:
            selector.close()
        if return_code != 0:
            raise subprocess.CalledProcessError(return_code, command)
        return digest.hexdigest()
    except BaseException:
        kill_unfinished_process(process)
        raise
    finally:
        if stdout is not None:
            stdout.close()


def wait_for_stdout(
    process: subprocess.Popen[bytes],
    selector: selectors.BaseSelector,
    command: list[str],
    deadline: float,
    timeout_seconds: float,
) -> None:
    remaining = deadline - monotonic()
    if remaining <= 0:
        kill_timed_out_process(process, command, timeout_seconds)
    if not selector.select(remaining):
        kill_timed_out_process(process, command, timeout_seconds)


def wait_for_exit(
    process: subprocess.Popen[bytes],
    command: list[str],
    deadline: float,
    timeout_seconds: float,
) -> int:
    remaining = deadline - monotonic()
    if remaining <= 0:
        kill_timed_out_process(process, command, timeout_seconds)
    try:
        return process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        kill_timed_out_process(process, command, timeout_seconds)


def kill_timed_out_process(
    process: subprocess.Popen[bytes],
    command: list[str],
    timeout_seconds: float,
) -> None:
    kill_unfinished_process(process)
    raise subprocess.TimeoutExpired(command, timeout_seconds)


def kill_unfinished_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait()
