from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from crewplane.core.workspace.git_policy import (
    sanitized_workspace_git_environment,
    workspace_git_config_args,
)

RUNTIME_GIT_COMMAND_TIMEOUT_SECONDS = 600.0


@dataclass(frozen=True)
class GitCommand:
    cwd: Path
    env: dict[str, str]
    timeout_seconds: float = RUNTIME_GIT_COMMAND_TIMEOUT_SECONDS

    def run(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        return self._run(args, None)

    def run_with_input(
        self,
        input_data: bytes,
        *args: str,
    ) -> subprocess.CompletedProcess[bytes]:
        return self._run(args, input_data)

    def _run(
        self,
        args: tuple[str, ...],
        input_data: bytes | None,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", *workspace_git_config_args(), "-C", self.cwd.as_posix(), *args],
            check=True,
            capture_output=True,
            env=self.env,
            input=input_data,
            timeout=self.timeout_seconds,
        )

    def text(self, *args: str) -> str:
        return self.run(*args).stdout.decode("utf-8", errors="replace").strip()

    def zero_records(self, *args: str) -> tuple[str, ...]:
        output = self.run(*args).stdout.decode("utf-8", errors="replace")
        return tuple(record for record in output.split("\0") if record)


def sanitized_git_env(index_path: Path | None = None) -> dict[str, str]:
    return sanitized_workspace_git_environment(index_path, read_only=True)


def git(
    cwd: Path,
    index_path: Path | None = None,
    timeout_seconds: float = RUNTIME_GIT_COMMAND_TIMEOUT_SECONDS,
) -> GitCommand:
    return GitCommand(
        cwd=cwd,
        env=sanitized_git_env(index_path),
        timeout_seconds=timeout_seconds,
    )


def git_error(exc: BaseException) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        stderr = exc.stderr.decode("utf-8", errors="replace").strip()
        return stderr or str(exc)
    return str(exc)
