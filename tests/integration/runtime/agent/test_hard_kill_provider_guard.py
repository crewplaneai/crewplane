from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest

from crewplane.artifacts.locks import ResumeLockError, acquire_same_context_lock
from tests.helpers.processes import kill_process_group
from tests.helpers.resume import (
    WORKFLOW_IDENTITY,
    WORKFLOW_NAME,
    WORKFLOW_SIGNATURE,
)


@pytest.mark.skipif(os.name != "posix", reason="Crewplane supports POSIX hosts")
def test_hard_killed_parent_blocks_restart_while_provider_is_alive(
    tmp_path: Path,
) -> None:
    parent = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tests.helpers.provider_guard_scenario",
            "running",
            str(tmp_path),
            WORKFLOW_NAME,
            WORKFLOW_IDENTITY,
            WORKFLOW_SIGNATURE,
        ],
        cwd=Path(__file__).resolve().parents[4],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    provider_pid: int | None = None
    try:
        state_path = _wait_for_provider_state(tmp_path, parent)
        provider_pid = json.loads(state_path.read_text(encoding="utf-8"))["pid"]
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=5)

        with pytest.raises(ResumeLockError, match="provider process"):
            acquire_same_context_lock(
                tmp_path,
                WORKFLOW_NAME,
                WORKFLOW_IDENTITY,
                WORKFLOW_SIGNATURE,
                grace_seconds=0,
            )

        os.kill(provider_pid, 0)
    finally:
        _cleanup_parent_and_provider(parent, tmp_path)


@pytest.mark.skipif(os.name != "posix", reason="Crewplane supports POSIX hosts")
def test_completed_provider_drains_descendant_before_publishing_exit(
    tmp_path: Path,
) -> None:
    descendant_pid_path = tmp_path / "provider-descendant.pid"
    parent = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tests.helpers.provider_guard_scenario",
            "descendant",
            str(tmp_path),
            WORKFLOW_NAME,
            WORKFLOW_IDENTITY,
            WORKFLOW_SIGNATURE,
        ],
        cwd=Path(__file__).resolve().parents[4],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    descendant_pid: int | None = None
    try:
        _wait_for_exited_provider_state(tmp_path, parent)
        descendant_pid = _wait_for_recorded_pid(descendant_pid_path, parent)
        with pytest.raises(ProcessLookupError):
            os.kill(descendant_pid, 0)

        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=5)

        lock = acquire_same_context_lock(
            tmp_path,
            WORKFLOW_NAME,
            WORKFLOW_IDENTITY,
            WORKFLOW_SIGNATURE,
            grace_seconds=0,
        )
        lock.release()
    finally:
        _cleanup_parent_and_provider(parent, tmp_path)


def _wait_for_provider_state(tmp_path: Path, parent: subprocess.Popen[str]) -> Path:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        states = tuple(
            (tmp_path / "execution-stages").glob(
                "*/manifests/provider-processes/*.json"
            )
        )
        if states:
            state_path = states[0]
            temporary_paths = state_path.parent.glob(f".{state_path.name}.*.tmp")
            if state_path.stat().st_nlink == 1 and not any(temporary_paths):
                return state_path
        if parent.poll() is not None:
            stderr = parent.communicate(timeout=10)[1]
            raise AssertionError(f"parent exited before provider launch: {stderr}")
        time.sleep(0.02)
    raise TimeoutError("provider process state was not published")


def _wait_for_exited_provider_state(
    tmp_path: Path,
    parent: subprocess.Popen[str],
) -> Path:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        states = tuple(
            (tmp_path / "execution-stages").glob(
                "*/manifests/provider-processes/*.json"
            )
        )
        if states:
            payload = json.loads(states[0].read_text(encoding="utf-8"))
            if payload["status"] == "exited":
                return states[0]
        if parent.poll() is not None:
            stderr = parent.communicate(timeout=10)[1]
            raise AssertionError(f"parent exited before provider completion: {stderr}")
        time.sleep(0.02)
    raise TimeoutError("provider process exit state was not published")


def _wait_for_recorded_pid(
    pid_path: Path,
    parent: subprocess.Popen[str],
) -> int:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            return int(pid_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        if parent.poll() is not None:
            stderr = parent.communicate(timeout=10)[1]
            raise AssertionError(f"parent exited before descendant launch: {stderr}")
        time.sleep(0.02)
    raise TimeoutError("provider descendant PID was not recorded")


def _recorded_provider_pid(tmp_path: Path) -> int | None:
    states = tuple(
        (tmp_path / "execution-stages").glob("*/manifests/provider-processes/*.json")
    )
    if not states:
        return None
    return int(json.loads(states[0].read_text(encoding="utf-8"))["pid"])


def _cleanup_parent_and_provider(parent: subprocess.Popen[str], root: Path) -> None:
    try:
        kill_process_group(parent.pid)
        parent.wait(timeout=10)
    finally:
        try:
            provider_pid = _recorded_provider_pid(root)
            if provider_pid is not None:
                kill_process_group(provider_pid)
            descendant_path = root / "provider-descendant.pid"
            if descendant_path.exists():
                descendant_pid = int(descendant_path.read_text(encoding="utf-8"))
                with suppress(ProcessLookupError):
                    kill_process_group(os.getpgid(descendant_pid))
        finally:
            if parent.stderr is not None:
                parent.stderr.close()
