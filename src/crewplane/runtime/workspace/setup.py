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

from crewplane.architecture.contracts import (
    ChildProcessEnvironment,
    JsonObject,
    JsonValue,
)
from crewplane.artifacts.atomic import atomic_write_json
from crewplane.core.platform import supports_posix_process_groups
from crewplane.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSelectionRecord,
)
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.preflight.serialization import to_json_safe
from crewplane.runtime.agent.process.drain import ProcessDrainError, drain_popen_process
from crewplane.runtime.agent.workspace_environment import (
    workspace_child_environment,
)
from crewplane.runtime.workspace.mutator_fence import (
    fence_workspace_mutator,
    release_workspace_mutator,
)
from crewplane.runtime.workspace.state_evidence import record_workspace_process_drain


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
    _state_path: Path | None = None

    def cancel(self) -> None:
        process: subprocess.Popen[str] | None = None
        with self._lock:
            self._cancelled = True
            process = self._process
            state_path = self._state_path
        if process is not None:
            _terminate_setup_process(process, state_path)

    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def register_process(
        self,
        process: subprocess.Popen[str],
        state_path: Path | None = None,
    ) -> bool:
        with self._lock:
            if self._cancelled:
                should_terminate = True
            else:
                self._process = process
                self._state_path = state_path
                should_terminate = False
        if should_terminate:
            _terminate_setup_process(process, state_path)
            return False
        return True

    def clear_process(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            if self._process is process:
                self._process = None
                self._state_path = None


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
    secret_context: SecretContext | None = None,
) -> JsonObject | None:
    setup = policy.setup
    if setup is None or not setup.commands:
        return None

    artifacts = workspace_setup_artifacts(state_path)
    process_state_path = state_path if state_path.is_file() else None
    child_environment = workspace_child_environment(cwd, checkout_root)
    timeout_seconds = _setup_timeout_seconds(plan)
    started_at = datetime.now(UTC).isoformat()
    started = time.monotonic()
    deadline = started + timeout_seconds
    records: list[JsonValue] = []
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
            resolved_argv = _resolve_setup_argv(command.argv, secret_context)
            record = _run_setup_command(
                resolved_argv,
                command.argv,
                command.command_index,
                cwd,
                remaining,
                child_environment,
                log_handle,
                cancellation,
                process_state_path,
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
    recorded_argv: list[str | JsonObject],
    command_index: int,
    cwd: Path,
    timeout_seconds: float,
    child_environment: ChildProcessEnvironment,
    log_handle: TextIO,
    cancellation: WorkspaceSetupCancellation | None,
    state_path: Path | None,
) -> JsonObject:
    started_at = datetime.now(UTC).isoformat()
    started = time.monotonic()
    log_handle.write(f"$ {_display_command(recorded_argv)}\n")
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
                state_path,
            )
            if cancelled:
                exit_code = None
                record_timed_out = False
                status_line = "[cancelled] true\n\n"
            elif timed_out:
                exit_code = None
                record_timed_out = True
                status_line = "[timed_out] true\n\n"
            else:
                exit_code = returncode
                record_timed_out = False
                status_line = f"[exit_code] {returncode}\n\n"
            _write_stream(log_handle, "stdout", stdout_file)
            _write_stream(log_handle, "stderr", stderr_file)
            log_handle.write(status_line)
            record = _setup_command_record(
                recorded_argv,
                command_index,
                cwd,
                started_at,
                started,
                exit_code=exit_code,
                timed_out=record_timed_out,
            )
            if cancelled:
                record["cancelled"] = True
            return record
    except OSError as exc:
        log_handle.write(f"[error] {exc}\n\n")
        record = _setup_command_record(
            recorded_argv,
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
    state_path: Path | None,
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
        registered = cancellation.register_process(process, state_path)
        if not registered:
            return None, False, True
    try:
        returncode = process.wait(timeout=timeout_seconds)
        _terminate_setup_process(process, state_path)
        if cancellation is not None and cancellation.is_cancelled():
            return None, False, True
        return returncode, False, False
    except subprocess.TimeoutExpired:
        _terminate_setup_process(process, state_path)
        return None, True, False
    finally:
        if cancellation is not None and registered:
            cancellation.clear_process(process)


def _terminate_setup_process(
    process: subprocess.Popen[str],
    state_path: Path | None = None,
) -> None:
    process_group_id = process.pid if supports_posix_process_groups() else None
    try:
        evidence = drain_popen_process(
            process,
            process_group_id,
            _send_setup_process_group_signal,
        )
    except ProcessDrainError as exc:
        if state_path is not None:
            _record_unresolved_process_drain(state_path, exc)
        raise
    if state_path is not None:
        record_workspace_process_drain(
            state_path,
            "confirmed",
            evidence.pid,
            evidence.process_group_id,
        )
        release_workspace_mutator(state_path)


def _record_unresolved_process_drain(
    state_path: Path,
    error: ProcessDrainError,
) -> None:
    fence_workspace_mutator(state_path)
    try:
        record_workspace_process_drain(
            state_path,
            "unresolved",
            error.evidence.pid,
            error.evidence.process_group_id,
            str(error),
        )
    except Exception as persistence_error:
        error.add_note(
            f"Workspace process-drain evidence persistence failed: {persistence_error}"
        )


def _send_setup_process_group_signal(
    process_group_id: int,
    termination_signal: signal.Signals,
) -> bool:
    try:
        os.killpg(process_group_id, termination_signal)
    except ProcessLookupError:
        return True
    return True


def _setup_command_record(
    argv: list[str | JsonObject],
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


def _resolve_setup_argv(
    argv: list[str | JsonObject],
    secret_context: SecretContext | None,
) -> list[str]:
    resolved: list[str] = []
    for token in argv:
        if isinstance(token, str):
            resolved.append(token)
            continue
        handle = token.get("value_handle")
        if not isinstance(handle, str):
            raise ValueError("Redacted workspace setup argv is missing a value handle.")
        if secret_context is None:
            raise ValueError(
                f"Workspace setup secret handle '{handle}' is unavailable."
            )
        try:
            resolved.append(secret_context.get(handle))
        except KeyError as exc:
            raise ValueError(
                f"Workspace setup secret handle '{handle}' is unavailable."
            ) from exc
    return resolved


def _display_command(argv: list[str | JsonObject]) -> str:
    return " ".join(token if isinstance(token, str) else "<redacted>" for token in argv)
