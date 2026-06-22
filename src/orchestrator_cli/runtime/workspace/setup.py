from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import TextIO

from orchestrator_cli.architecture.contracts import ChildProcessEnvironment, JsonObject
from orchestrator_cli.artifacts.atomic import atomic_write_json
from orchestrator_cli.core.platform import supports_posix_process_groups
from orchestrator_cli.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSelectionRecord,
)
from orchestrator_cli.core.preflight.serialization import to_json_safe
from orchestrator_cli.runtime.agent.workspace_environment import (
    workspace_child_environment,
)

SETUP_PROCESS_TERMINATION_GRACE_SECONDS = 5.0


class WorkspaceSetupError(RuntimeError):
    """Raised when a selected workspace setup profile fails before invocation."""

    def __init__(self, message: str, summary: JsonObject) -> None:
        super().__init__(message)
        self.summary = summary


class WorkspaceSetupCancelled(WorkspaceSetupError):
    """Raised when workspace setup is cancelled before invocation."""


@dataclass
class WorkspaceSetupCancellation:
    _lock: Lock = field(default_factory=Lock)
    _cancelled: bool = False
    _process: subprocess.Popen[str] | None = None

    def cancel(self) -> None:
        process: subprocess.Popen[str] | None = None
        with self._lock:
            self._cancelled = True
            process = self._process
        if process is not None:
            _terminate_setup_process(process)

    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def register_process(self, process: subprocess.Popen[str]) -> bool:
        with self._lock:
            if self._cancelled:
                should_terminate = True
            else:
                self._process = process
                should_terminate = False
        if should_terminate:
            _terminate_setup_process(process)
            return False
        return True

    def clear_process(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            if self._process is process:
                self._process = None


@dataclass(frozen=True)
class WorkspaceSetupArtifacts:
    metadata_path: Path
    log_path: Path


def run_workspace_setup(
    plan: PreflightExecutionPlan,
    policy: WorkspaceSelectionRecord,
    cwd: Path,
    state_path: Path,
    checkout_root: Path | None = None,
    cancellation: WorkspaceSetupCancellation | None = None,
) -> JsonObject | None:
    setup = policy.setup
    if setup is None or not setup.commands:
        return None

    artifacts = workspace_setup_artifacts(state_path)
    child_environment = workspace_child_environment(cwd, checkout_root)
    timeout_seconds = _setup_timeout_seconds(plan)
    started_at = datetime.now(UTC).isoformat()
    started = time.monotonic()
    deadline = started + timeout_seconds
    records: list[JsonObject] = []
    status = "succeeded"
    timed_out = False
    failure_message: str | None = None

    artifacts.log_path.parent.mkdir(parents=True, exist_ok=True)
    with artifacts.log_path.open("w", encoding="utf-8") as log_handle:
        for command in setup.commands:
            if cancellation is not None and cancellation.is_cancelled():
                status = "cancelled"
                failure_message = "Workspace setup profile was cancelled."
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                status = "timed_out"
                timed_out = True
                failure_message = "Workspace setup profile timed out."
                break
            record = _run_setup_command(
                command.argv,
                command.command_index,
                cwd,
                remaining,
                child_environment,
                log_handle,
                cancellation,
            )
            records.append(record)
            if record.get("cancelled") is True:
                status = "cancelled"
                failure_message = (
                    "Workspace setup command was cancelled: "
                    f"{_display_command(command.argv)}"
                )
                break
            if record.get("timed_out") is True:
                status = "timed_out"
                timed_out = True
                failure_message = (
                    "Workspace setup command timed out: "
                    f"{_display_command(command.argv)}"
                )
                break
            exit_code = record.get("exit_code")
            if exit_code != 0:
                status = "failed"
                failure_message = (
                    "Workspace setup command failed with exit code "
                    f"{exit_code}: {_display_command(command.argv)}"
                )
                break

    if (
        status == "succeeded"
        and cancellation is not None
        and cancellation.is_cancelled()
    ):
        status = "cancelled"
        failure_message = "Workspace setup profile was cancelled."

    completed_at = datetime.now(UTC).isoformat()
    summary: JsonObject = {
        "profile_name": setup.profile_name,
        "status": status,
        "timed_out": timed_out,
        "timeout_seconds": timeout_seconds,
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_seconds": round(time.monotonic() - started, 6),
        "commands": records,
        "log_path": artifacts.log_path.relative_to(state_path.parent).as_posix(),
        "metadata_path": artifacts.metadata_path.relative_to(
            state_path.parent
        ).as_posix(),
    }
    if failure_message is not None:
        summary["failure_message"] = failure_message

    atomic_write_json(artifacts.metadata_path, to_json_safe(summary))
    if status == "cancelled":
        raise WorkspaceSetupCancelled(
            failure_message or "Workspace setup was cancelled.",
            summary,
        )
    if status != "succeeded":
        raise WorkspaceSetupError(
            failure_message or "Workspace setup failed.",
            summary,
        )
    return summary


def workspace_setup_artifacts(state_path: Path) -> WorkspaceSetupArtifacts:
    setup_dir = state_path.parent / "workspace-setup"
    if state_path.name == "workspace-state.json":
        return WorkspaceSetupArtifacts(
            metadata_path=setup_dir / "setup.json",
            log_path=setup_dir / "setup.log",
        )
    return WorkspaceSetupArtifacts(
        metadata_path=setup_dir / f"{state_path.stem}.json",
        log_path=setup_dir / f"{state_path.stem}.log",
    )


def _run_setup_command(
    argv: list[str],
    command_index: int,
    cwd: Path,
    timeout_seconds: float,
    child_environment: ChildProcessEnvironment,
    log_handle: TextIO,
    cancellation: WorkspaceSetupCancellation | None,
) -> JsonObject:
    started_at = datetime.now(UTC).isoformat()
    started = time.monotonic()
    log_handle.write(f"$ {_display_command(argv)}\n")
    try:
        with (
            tempfile.TemporaryFile(
                "w+",
                encoding="utf-8",
            ) as stdout_file,
            tempfile.TemporaryFile(
                "w+",
                encoding="utf-8",
            ) as stderr_file,
        ):
            returncode, timed_out, cancelled = _run_setup_process(
                argv,
                cwd,
                timeout_seconds,
                child_environment,
                stdout_file,
                stderr_file,
                cancellation,
            )
            if cancelled:
                _write_stream(log_handle, "stdout", stdout_file)
                _write_stream(log_handle, "stderr", stderr_file)
                log_handle.write("[cancelled] true\n\n")
                record = _setup_command_record(
                    argv,
                    command_index,
                    cwd,
                    started_at,
                    started,
                    exit_code=None,
                    timed_out=False,
                )
                record["cancelled"] = True
                return record
            if timed_out:
                _write_stream(log_handle, "stdout", stdout_file)
                _write_stream(log_handle, "stderr", stderr_file)
                log_handle.write("[timed_out] true\n\n")
                return _setup_command_record(
                    argv,
                    command_index,
                    cwd,
                    started_at,
                    started,
                    exit_code=None,
                    timed_out=True,
                )
            _write_stream(log_handle, "stdout", stdout_file)
            _write_stream(log_handle, "stderr", stderr_file)
            log_handle.write(f"[exit_code] {returncode}\n\n")
            return _setup_command_record(
                argv,
                command_index,
                cwd,
                started_at,
                started,
                exit_code=returncode,
                timed_out=False,
            )
    except OSError as exc:
        log_handle.write(f"[error] {exc}\n\n")
        record = _setup_command_record(
            argv,
            command_index,
            cwd,
            started_at,
            started,
            exit_code=None,
            timed_out=False,
        )
        record["error"] = str(exc)
        return record


def _run_setup_process(
    argv: list[str],
    cwd: Path,
    timeout_seconds: float,
    child_environment: ChildProcessEnvironment,
    stdout_file: TextIO,
    stderr_file: TextIO,
    cancellation: WorkspaceSetupCancellation | None,
) -> tuple[int | None, bool, bool]:
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        stdout=stdout_file,
        stderr=stderr_file,
        env=_setup_child_env(child_environment),
        text=True,
        start_new_session=supports_posix_process_groups(),
    )
    registered = True
    if cancellation is not None:
        registered = cancellation.register_process(process)
        if not registered:
            return None, False, True
    try:
        returncode = process.wait(timeout=timeout_seconds)
        if cancellation is not None and cancellation.is_cancelled():
            return None, False, True
        return returncode, False, False
    except subprocess.TimeoutExpired:
        _terminate_setup_process(process)
        return None, True, False
    finally:
        if cancellation is not None and registered:
            cancellation.clear_process(process)


def _terminate_setup_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    terminate_process_group = supports_posix_process_groups()
    if terminate_process_group:
        _send_setup_process_group_signal(process, signal.SIGTERM)
    else:
        process.terminate()
    try:
        process.wait(timeout=SETUP_PROCESS_TERMINATION_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        if terminate_process_group:
            _send_setup_process_group_signal(process, signal.SIGKILL)
        else:
            process.kill()
        process.wait()


def _send_setup_process_group_signal(
    process: subprocess.Popen[str],
    termination_signal: signal.Signals,
) -> None:
    try:
        os.killpg(os.getpgid(process.pid), termination_signal)
    except ProcessLookupError:
        return


def _setup_command_record(
    argv: list[str],
    command_index: int,
    cwd: Path,
    started_at: str,
    started: float,
    exit_code: int | None,
    timed_out: bool,
) -> JsonObject:
    return {
        "argv": list(argv),
        "command_index": command_index,
        "working_directory": cwd.as_posix(),
        "started_at": started_at,
        "completed_at": datetime.now(UTC).isoformat(),
        "duration_seconds": round(time.monotonic() - started, 6),
        "exit_code": exit_code,
        "timed_out": timed_out,
    }


def _setup_timeout_seconds(plan: PreflightExecutionPlan) -> float:
    workspace = plan.runtime_config_snapshot.get("workspace")
    if not isinstance(workspace, dict):
        return 600.0
    value = workspace.get("setup_timeout_seconds")
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return float(value)
    return 600.0


def _setup_child_env(child_environment: ChildProcessEnvironment) -> dict[str, str]:
    env = dict(os.environ)
    for key in child_environment.unset:
        env.pop(key, None)
    env.update(child_environment.set)
    return env


def _write_stream(log_handle: TextIO, name: str, stream: TextIO) -> None:
    stream.seek(0, os.SEEK_END)
    if stream.tell() == 0:
        return
    stream.seek(0)
    log_handle.write(f"[{name}]\n")
    for chunk in iter(lambda: stream.read(1024 * 1024), ""):
        log_handle.write(chunk)
    log_handle.write("\n")


def _display_command(argv: list[str]) -> str:
    return " ".join(argv)
