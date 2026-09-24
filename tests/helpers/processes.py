"""Bounded execution and cleanup for test-owned process groups."""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path

PROCESS_TIMEOUT_SECONDS = 30.0


def kill_process_group(process_group_id: int) -> None:
    assert process_group_id != os.getpgrp(), "Refusing to kill the pytest process group"
    with suppress(ProcessLookupError):
        os.killpg(process_group_id, signal.SIGKILL)


def run_process(
    command: Sequence[str],
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = False,
    timeout: float = PROCESS_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    with subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            kill_process_group(process.pid)
            stdout, stderr = process.communicate(timeout=PROCESS_TIMEOUT_SECONDS)
            raise AssertionError(
                f"{list(command)!r} exceeded {timeout}s.\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            ) from error
        finally:
            kill_process_group(process.pid)
        result = subprocess.CompletedProcess(
            command, process.returncode, stdout, stderr
        )
    if check:
        result.check_returncode()
    return result
