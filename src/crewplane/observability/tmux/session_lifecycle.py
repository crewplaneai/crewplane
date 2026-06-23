from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from crewplane.observability.tmux.client import (
    TmuxCommandClient,
    TmuxSessionClient,
)
from crewplane.observability.tmux.commands import (
    build_attach_command,
    pane_render_command,
)
from crewplane.observability.tmux.runtime_files import (
    RuntimeFiles,
    initial_runtime_file_contents,
    write_atomic,
)
from crewplane.observability.tmux.session import (
    TmuxSessionIdentity,
    TmuxSessionTargets,
)
from crewplane.observability.types import RunContext

TmuxClientFactory = Callable[[str | None], TmuxSessionClient]


@dataclass
class RuntimeDirectoryLease:
    root: Path
    preserve_on_stop: bool
    _cleaned: bool = False

    def cleanup(self, force: bool = False) -> None:
        if self._cleaned:
            return
        if self.preserve_on_stop and not force:
            return
        if self.root.exists():
            shutil.rmtree(self.root)
        self._cleaned = True


@dataclass
class StartedCompactSession:
    runtime_lease: RuntimeDirectoryLease
    runtime_files: RuntimeFiles
    targets: TmuxSessionTargets
    tmux: TmuxSessionClient
    attach_process: subprocess.Popen[str] | None = None
    attach_attempted: bool = False


class CompactSessionLifecycle(Protocol):
    def create_session(self, context: RunContext) -> StartedCompactSession: ...

    def attach_or_switch(self, session: StartedCompactSession) -> None: ...

    def rollback_start(self, session: StartedCompactSession | None) -> None: ...

    def stop_session(
        self,
        session: StartedCompactSession | None,
        auto_close_session: bool,
    ) -> None: ...


class TmuxCompactSessionLifecycle:
    """Own session creation, attach/switch, rollback, and stop behavior."""

    def __init__(
        self,
        auto_close_session: bool = True,
        tmux_executable: str = "tmux",
        refresh_interval_seconds: float = 0.25,
        warning_sink: Callable[[str], None] | None = None,
        tmux_command_timeout_seconds: float = 1.0,
        client_factory: TmuxClientFactory | None = None,
    ) -> None:
        self._preserve_on_stop = not auto_close_session
        self._tmux_executable = tmux_executable
        self._refresh_interval_seconds = refresh_interval_seconds
        self._warning_sink = warning_sink
        self._client_factory = client_factory or (
            lambda socket_name: TmuxCommandClient(
                tmux_executable=tmux_executable,
                socket_name=socket_name,
                warning_sink=warning_sink,
                timeout_seconds=tmux_command_timeout_seconds,
            )
        )

    def create_session(self, context: RunContext) -> StartedCompactSession:
        lease = RuntimeDirectoryLease(
            root=Path(
                tempfile.mkdtemp(prefix=f"crewplane-tmux-compact-{context.run_id}-")
            ),
            preserve_on_stop=self._preserve_on_stop,
        )
        runtime_files = RuntimeFiles.from_root(lease.root)
        identity = TmuxSessionIdentity.from_run(
            context.run_id,
            socket_name=lease.root.name,
        )
        tmux = self._client_factory(identity.socket_name)
        try:
            self._initialize_runtime_files(runtime_files)
            targets = self._create_tmux_panes(identity, runtime_files, tmux)
            return StartedCompactSession(
                runtime_lease=lease,
                runtime_files=runtime_files,
                targets=targets,
                tmux=tmux,
            )
        except Exception:
            self._rollback_partial_start(lease, tmux, identity)
            raise

    def attach_or_switch(self, session: StartedCompactSession) -> None:
        session.attach_attempted = True
        targets = session.targets
        if os.environ.get("TMUX"):
            switch_result = session.tmux.run(
                ["switch-client", "-t", targets.session_name],
                check=False,
            )
            if switch_result.returncode == 0:
                return

        last_failure = self._try_attach_candidates(session)
        if last_failure is None:
            return
        raise RuntimeError(
            f"Failed to attach tmux session '{targets.session_name}': {last_failure}"
        )

    def rollback_start(self, session: StartedCompactSession | None) -> None:
        if session is None:
            return
        if session.attach_process is not None:
            _terminate_attach_process(session.attach_process)
            session.attach_process = None
        try:
            session.tmux.run(
                ["kill-session", "-t", session.targets.session_name],
                check=False,
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            self._warn(f"tmux compact rollback failed: {exc}")
        self._cleanup_lease(session.runtime_lease, force=True)

    def stop_session(
        self,
        session: StartedCompactSession | None,
        auto_close_session: bool,
    ) -> None:
        if session is None or not auto_close_session:
            return

        failure: Exception | None = None
        try:
            session.tmux.run(
                ["kill-session", "-t", session.targets.session_name],
                check=False,
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            failure = exc
        try:
            if session.attach_process is not None:
                _terminate_attach_process(session.attach_process)
                session.attach_process = None
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            failure = failure or exc
        try:
            session.runtime_lease.cleanup()
        except OSError as exc:
            failure = failure or exc
        if failure is not None:
            raise failure

    def _initialize_runtime_files(self, runtime_files: RuntimeFiles) -> None:
        for path, content in initial_runtime_file_contents(runtime_files).items():
            write_atomic(path, content)

    def _create_tmux_panes(
        self,
        identity: TmuxSessionIdentity,
        runtime_files: RuntimeFiles,
        tmux: TmuxSessionClient,
    ) -> TmuxSessionTargets:
        left_pane = self._create_left_pane(identity, runtime_files, tmux)
        right_pane = self._create_right_pane(left_pane, runtime_files, tmux)
        return TmuxSessionTargets.from_identity(
            identity,
            left_pane_id=left_pane,
            right_pane_id=right_pane,
        )

    def _create_left_pane(
        self,
        identity: TmuxSessionIdentity,
        runtime_files: RuntimeFiles,
        tmux: TmuxSessionClient,
    ) -> str:
        result = tmux.run(
            [
                "new-session",
                "-d",
                "-P",
                "-F",
                "#{pane_id}",
                "-s",
                identity.session_name,
                "-n",
                "dashboard",
                pane_render_command(
                    runtime_files.left_content,
                    self._refresh_interval_seconds,
                ),
            ],
            capture_output=True,
        )
        pane_id = result.stdout.strip()
        if not pane_id:
            raise RuntimeError("Failed to create tmux compact dashboard pane.")
        return pane_id

    def _create_right_pane(
        self,
        left_pane: str,
        runtime_files: RuntimeFiles,
        tmux: TmuxSessionClient,
    ) -> str:
        result = tmux.run(
            [
                "split-window",
                "-d",
                "-h",
                "-p",
                "55",
                "-P",
                "-F",
                "#{pane_id}",
                "-t",
                left_pane,
                pane_render_command(
                    runtime_files.right_content,
                    self._refresh_interval_seconds,
                ),
            ],
            capture_output=True,
        )
        pane_id = result.stdout.strip()
        if not pane_id:
            raise RuntimeError("Failed to create tmux compact output pane.")
        return pane_id

    def _try_attach_candidates(
        self,
        session: StartedCompactSession,
    ) -> str | None:
        last_failure: str | None = None
        for command, env in self._attach_candidates(session.targets):
            process = subprocess.Popen(command, text=True, env=env)
            try:
                returncode = process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                session.attach_process = process
                return None
            if returncode == 0:
                session.attach_process = None
                return None
            last_failure = f"{' '.join(command)} exited with code {returncode}"
        return last_failure

    def _attach_candidates(
        self,
        targets: TmuxSessionTargets,
    ) -> list[tuple[list[str], dict[str, str] | None]]:
        attach_command = build_attach_command(
            session_name=targets.session_name,
            tmux_executable=self._tmux_executable,
            socket_name=targets.socket_name,
        )
        candidates: list[tuple[list[str], dict[str, str] | None]] = [
            (attach_command, None)
        ]
        plain_attach = [self._tmux_executable]
        if targets.socket_name:
            plain_attach.extend(["-L", targets.socket_name])
        plain_attach.extend(["attach", "-t", targets.session_name])
        if attach_command != plain_attach:
            candidates.append((plain_attach, None))
        if not os.environ.get("TMUX"):
            return candidates

        attach_env = dict(os.environ)
        attach_env.pop("TMUX", None)
        return [(command, attach_env) for command, _ in candidates]

    def _rollback_partial_start(
        self,
        lease: RuntimeDirectoryLease,
        tmux: TmuxSessionClient,
        identity: TmuxSessionIdentity,
    ) -> None:
        try:
            tmux.run(["kill-session", "-t", identity.session_name], check=False)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            self._warn(f"tmux compact rollback failed: {exc}")
        self._cleanup_lease(lease, force=True)

    def _cleanup_lease(self, lease: RuntimeDirectoryLease, force: bool) -> None:
        try:
            lease.cleanup(force=force)
        except OSError as exc:
            self._warn(f"tmux compact temp cleanup failed: {exc}")

    def _warn(self, message: str) -> None:
        if self._warning_sink is not None:
            try:
                self._warning_sink(message)
            except Exception:
                return
            return
        print(f"WARN: {message}", file=sys.stderr)


def _terminate_attach_process(process: subprocess.Popen[str]) -> None:
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1.0)
