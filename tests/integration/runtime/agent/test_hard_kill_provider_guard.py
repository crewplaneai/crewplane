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
            "-c",
            _PARENT_SCRIPT,
            str(tmp_path),
            WORKFLOW_NAME,
            WORKFLOW_IDENTITY,
            WORKFLOW_SIGNATURE,
        ],
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
        if parent.poll() is None:
            os.kill(parent.pid, signal.SIGKILL)
            parent.wait(timeout=5)
        if provider_pid is None:
            provider_pid = _recorded_provider_pid(tmp_path)
        if provider_pid is not None:
            with suppress(ProcessLookupError):
                os.killpg(provider_pid, signal.SIGKILL)
        if parent.stderr is not None:
            parent.stderr.close()


@pytest.mark.skipif(os.name != "posix", reason="Crewplane supports POSIX hosts")
def test_hard_killed_parent_blocks_restart_while_provider_descendant_is_alive(
    tmp_path: Path,
) -> None:
    descendant_pid_path = tmp_path / "provider-descendant.pid"
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _DESCENDANT_PARENT_SCRIPT,
            str(tmp_path),
            WORKFLOW_NAME,
            WORKFLOW_IDENTITY,
            WORKFLOW_SIGNATURE,
            str(descendant_pid_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    process_group_id: int | None = None
    descendant_pid: int | None = None
    try:
        state_path = _wait_for_exited_provider_state(tmp_path, parent)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        process_group_id = state["process_group_id"]
        descendant_pid = _wait_for_recorded_pid(descendant_pid_path, parent)
        os.kill(descendant_pid, 0)

        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=5)

        with pytest.raises(ResumeLockError, match="provider process group"):
            acquire_same_context_lock(
                tmp_path,
                WORKFLOW_NAME,
                WORKFLOW_IDENTITY,
                WORKFLOW_SIGNATURE,
                grace_seconds=0,
            )

        os.kill(descendant_pid, 0)
    finally:
        if parent.poll() is None:
            os.kill(parent.pid, signal.SIGKILL)
            parent.wait(timeout=5)
        if process_group_id is not None:
            with suppress(ProcessLookupError):
                os.killpg(process_group_id, signal.SIGKILL)
        elif descendant_pid is not None:
            with suppress(ProcessLookupError):
                os.kill(descendant_pid, signal.SIGKILL)
        if parent.stderr is not None:
            parent.stderr.close()


def _wait_for_provider_state(tmp_path: Path, parent: subprocess.Popen[str]) -> Path:
    deadline = time.monotonic() + 5
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
            stderr = parent.stderr.read() if parent.stderr is not None else ""
            raise AssertionError(f"parent exited before provider launch: {stderr}")
        time.sleep(0.02)
    raise TimeoutError("provider process state was not published")


def _wait_for_exited_provider_state(
    tmp_path: Path,
    parent: subprocess.Popen[str],
) -> Path:
    deadline = time.monotonic() + 5
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
            stderr = parent.stderr.read() if parent.stderr is not None else ""
            raise AssertionError(f"parent exited before provider completion: {stderr}")
        time.sleep(0.02)
    raise TimeoutError("provider process exit state was not published")


def _wait_for_recorded_pid(
    pid_path: Path,
    parent: subprocess.Popen[str],
) -> int:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            return int(pid_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        if parent.poll() is not None:
            stderr = parent.stderr.read() if parent.stderr is not None else ""
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


_PARENT_SCRIPT = r"""
import asyncio
import sys
from pathlib import Path

from crewplane.artifacts.locks import acquire_same_context_lock
from crewplane.artifacts.manager import OutputManager
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.agent.invocation.command import run_command_once
from crewplane.runtime.execution.activity.events import InvocationMetadata
from crewplane.runtime.execution.provider_call.display import ProviderCallDisplay
from crewplane.runtime.execution.provider_call.events import build_invocation_context

state_dir = Path(sys.argv[1])
workflow_name = sys.argv[2]
workflow_identity = sys.argv[3]
workflow_signature = sys.argv[4]
lock = acquire_same_context_lock(
    state_dir,
    workflow_name,
    workflow_identity,
    workflow_signature,
    grace_seconds=0,
)
output = OutputManager(workflow_name, base_dir=state_dir)
lock.update_run(output.run_id, output.run_key_name)
context, _ = build_invocation_context(
    telemetry=None,
    metadata=InvocationMetadata(
        node_id="build.node",
        provider="generic",
        role=ProviderRole.EXECUTOR,
        model=None,
        task_id="generic_executor_0",
        audit_round_num=None,
        round_num=1,
        output_file=output.stages_dir / "provider-output.md",
        log_file=None,
    ),
    display=ProviderCallDisplay(telemetry=None),
    output=output,
)
asyncio.run(
    run_command_once(
        cmd=[sys.executable, "-c", "import time; time.sleep(60)"],
        stdin_data=None,
        log_file=None,
        append_log=False,
        log_header=None,
        cwd=state_dir,
        invocation_context=context,
        idle_timeout_seconds=None,
    )
)
"""


_DESCENDANT_PARENT_SCRIPT = r"""
import asyncio
import subprocess
import sys
import time
from pathlib import Path

from crewplane.artifacts.locks import acquire_same_context_lock
from crewplane.artifacts.manager import OutputManager
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.agent.invocation.command import run_command_once
from crewplane.runtime.execution.activity.events import InvocationMetadata
from crewplane.runtime.execution.provider_call.display import ProviderCallDisplay
from crewplane.runtime.execution.provider_call.events import build_invocation_context

state_dir = Path(sys.argv[1])
workflow_name = sys.argv[2]
workflow_identity = sys.argv[3]
workflow_signature = sys.argv[4]
descendant_pid_path = Path(sys.argv[5])
lock = acquire_same_context_lock(
    state_dir,
    workflow_name,
    workflow_identity,
    workflow_signature,
    grace_seconds=0,
)
output = OutputManager(workflow_name, base_dir=state_dir)
lock.update_run(output.run_id, output.run_key_name)
context, _ = build_invocation_context(
    telemetry=None,
    metadata=InvocationMetadata(
        node_id="build.node",
        provider="generic",
        role=ProviderRole.EXECUTOR,
        model=None,
        task_id="generic_executor_0",
        audit_round_num=None,
        round_num=1,
        output_file=output.stages_dir / "provider-output.md",
        log_file=None,
    ),
    display=ProviderCallDisplay(telemetry=None),
    output=output,
)
provider_script = r'''\
import subprocess
import sys
from pathlib import Path

descendant = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
Path(sys.argv[1]).write_text(str(descendant.pid), encoding="utf-8")
'''
asyncio.run(
    run_command_once(
        cmd=[sys.executable, "-c", provider_script, str(descendant_pid_path)],
        stdin_data=None,
        log_file=None,
        append_log=False,
        log_header=None,
        cwd=state_dir,
        invocation_context=context,
        idle_timeout_seconds=None,
    )
)
time.sleep(60)
"""
