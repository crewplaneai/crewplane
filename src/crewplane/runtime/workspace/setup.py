from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Literal, NotRequired, TextIO, TypedDict, cast

from crewplane.architecture.contracts import (
    ChildProcessEnvironment,
    JsonObject,
)
from crewplane.artifacts.atomic import atomic_write_json
from crewplane.artifacts.workspace.state.paths import WORKSPACE_STATE_FILENAME
from crewplane.core.platform import supports_posix_process_groups
from crewplane.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSelectionRecord,
    WorkspaceSetupRecord,
)
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.preflight.serialization import to_json_safe
from crewplane.runtime.agent.process.drain import ProcessDrainError, drain_popen_process
from crewplane.runtime.agent.workspace_environment import (
    workspace_child_environment,
)
from crewplane.runtime.workspace.state_evidence import (
    confirm_workspace_process_drain,
    record_unresolved_workspace_process_drain,
)


class _SetupCommandRecord(TypedDict):
    argv: list[str | JsonObject]
    command_index: int
    working_directory: str
    started_at: str
    completed_at: str
    duration_seconds: float
    exit_code: int | None
    timed_out: bool
    cancelled: NotRequired[bool]
    error: NotRequired[str]


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


type _SetupFailure = tuple[Literal["cancelled", "timed_out", "failed"], str]


@dataclass(frozen=True)
class _SetupProfileResult:
    records: list[_SetupCommandRecord]
    status: Literal["succeeded", "cancelled", "timed_out", "failed"] = "succeeded"
    failure_message: str | None = None


@dataclass(frozen=True)
class _SetupProfileContext:
    setup: WorkspaceSetupRecord
    cwd: Path
    process_state_path: Path | None
    child_environment: ChildProcessEnvironment
    timeout_seconds: float
    started_at: str
    started: float
    cancellation: WorkspaceSetupCancellation | None
    secret_context: SecretContext | None

    @property
    def deadline(self) -> float:
        return self.started + self.timeout_seconds


def run_workspace_setup(
    plan: PreflightExecutionPlan,
    policy: WorkspaceSelectionRecord,
    cwd: Path,
    state_path: Path,
    checkout_root: Path | None = None,
    cancellation: WorkspaceSetupCancellation | None = None,
    secret_context: SecretContext | None = None,
) -> JsonObject | None:
    """Run the selected setup profile and publish its summary before reporting failure."""

    setup = policy.setup
    if setup is None or not setup.commands:
        return None

    artifacts = workspace_setup_artifacts(state_path)
    context = _SetupProfileContext(
        setup=setup,
        cwd=cwd,
        process_state_path=state_path if state_path.is_file() else None,
        child_environment=workspace_child_environment(cwd, checkout_root),
        timeout_seconds=_setup_timeout_seconds(plan),
        started_at=datetime.now(UTC).isoformat(),
        started=time.monotonic(),
        cancellation=cancellation,
        secret_context=secret_context,
    )

    artifacts.log_path.parent.mkdir(parents=True, exist_ok=True)
    with artifacts.log_path.open("w", encoding="utf-8") as log_handle:
        result = _run_setup_commands(context, log_handle)

    if (
        result.status == "succeeded"
        and cancellation is not None
        and cancellation.is_cancelled()
    ):
        result = replace(
            result,
            status="cancelled",
            failure_message="Workspace setup profile was cancelled.",
        )

    summary = _build_setup_summary(context, result, artifacts, state_path.parent)
    atomic_write_json(artifacts.metadata_path, to_json_safe(summary))
    if result.status == "cancelled":
        raise WorkspaceSetupCancelled(
            result.failure_message or "Workspace setup was cancelled.", summary
        )
    if result.status != "succeeded":
        raise WorkspaceSetupError(
            result.failure_message or "Workspace setup failed.", summary
        )
    return summary


def _run_setup_commands(
    context: _SetupProfileContext,
    log_handle: TextIO,
) -> _SetupProfileResult:
    records: list[_SetupCommandRecord] = []
    for command in context.setup.commands:
        if context.cancellation is not None and context.cancellation.is_cancelled():
            return _SetupProfileResult(
                records, "cancelled", "Workspace setup profile was cancelled."
            )
        remaining = context.deadline - time.monotonic()
        if remaining <= 0:
            return _SetupProfileResult(
                records, "timed_out", "Workspace setup profile timed out."
            )
        resolved_argv = _resolve_setup_argv(command.argv, context.secret_context)
        record = _run_setup_command(
            resolved_argv,
            command.argv,
            command.command_index,
            context.cwd,
            remaining,
            context.child_environment,
            log_handle,
            context.cancellation,
            context.process_state_path,
        )
        records.append(record)
        failure = _setup_command_failure(record)
        if failure is not None:
            status, message = failure
            return _SetupProfileResult(records, status, message)
    return _SetupProfileResult(records)


def _setup_command_failure(record: _SetupCommandRecord) -> _SetupFailure | None:
    if record.get("cancelled") is True:
        return (
            "cancelled",
            f"Workspace setup command was cancelled: {_display_command(record['argv'])}",
        )
    if record["timed_out"]:
        return (
            "timed_out",
            f"Workspace setup command timed out: {_display_command(record['argv'])}",
        )
    if record["exit_code"] != 0:
        return (
            "failed",
            "Workspace setup command failed with exit code "
            f"{record['exit_code']}: {_display_command(record['argv'])}",
        )
    return None


def _build_setup_summary(
    context: _SetupProfileContext,
    result: _SetupProfileResult,
    artifacts: WorkspaceSetupArtifacts,
    state_dir: Path,
) -> JsonObject:
    completed_at = datetime.now(UTC).isoformat()
    summary: JsonObject = {
        "profile_name": context.setup.profile_name,
        "status": result.status,
        "timed_out": result.status == "timed_out",
        "timeout_seconds": context.timeout_seconds,
        "started_at": context.started_at,
        "completed_at": completed_at,
        "duration_seconds": round(time.monotonic() - context.started, 6),
        "commands": [cast(JsonObject, record) for record in result.records],
        "log_path": artifacts.log_path.relative_to(state_dir).as_posix(),
        "metadata_path": artifacts.metadata_path.relative_to(state_dir).as_posix(),
    }
    if result.failure_message is not None:
        summary["failure_message"] = result.failure_message
    return summary


def workspace_setup_artifacts(state_path: Path) -> WorkspaceSetupArtifacts:
    setup_dir = state_path.parent / "workspace-setup"
    if state_path.name == WORKSPACE_STATE_FILENAME:
        return WorkspaceSetupArtifacts(
            metadata_path=setup_dir / "setup.json",
            log_path=setup_dir / "setup.log",
        )
    return WorkspaceSetupArtifacts(
        metadata_path=setup_dir / f"{state_path.stem}.json",
        log_path=setup_dir / f"{state_path.stem}.log",
    )


@dataclass(frozen=True)
class _SetupProcessResult:
    exit_code: int | None
    timed_out: bool = False
    cancelled: bool = False


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
) -> _SetupCommandRecord:
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
            result = _run_setup_process(
                argv,
                cwd,
                timeout_seconds,
                child_environment,
                stdout_file,
                stderr_file,
                cancellation,
                state_path,
            )
            _write_setup_command_output(log_handle, stdout_file, stderr_file, result)
            record = _setup_command_record(
                recorded_argv,
                command_index,
                cwd,
                started_at,
                started,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
            )
            if result.cancelled:
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


def _write_setup_command_output(
    log_handle: TextIO,
    stdout_file: TextIO,
    stderr_file: TextIO,
    result: _SetupProcessResult,
) -> None:
    _write_stream(log_handle, "stdout", stdout_file)
    _write_stream(log_handle, "stderr", stderr_file)
    if result.cancelled:
        log_handle.write("[cancelled] true\n\n")
    elif result.timed_out:
        log_handle.write("[timed_out] true\n\n")
    else:
        log_handle.write(f"[exit_code] {result.exit_code}\n\n")


def _run_setup_process(
    argv: list[str],
    cwd: Path,
    timeout_seconds: float,
    child_environment: ChildProcessEnvironment,
    stdout_file: TextIO,
    stderr_file: TextIO,
    cancellation: WorkspaceSetupCancellation | None,
    state_path: Path | None,
) -> _SetupProcessResult:
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
            return _SetupProcessResult(None, cancelled=True)
    try:
        returncode = process.wait(timeout=timeout_seconds)
        _terminate_setup_process(process, state_path)
        if cancellation is not None and cancellation.is_cancelled():
            return _SetupProcessResult(None, cancelled=True)
        return _SetupProcessResult(returncode)
    except subprocess.TimeoutExpired:
        _terminate_setup_process(process, state_path)
        return _SetupProcessResult(None, timed_out=True)
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
        record_unresolved_workspace_process_drain(state_path, exc)
        raise
    confirm_workspace_process_drain(state_path, evidence.pid, evidence.process_group_id)


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
) -> _SetupCommandRecord:
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
