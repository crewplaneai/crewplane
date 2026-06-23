from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from crewplane.core.workspace.git_policy import (
    sanitized_workspace_git_environment,
    workspace_git_config_args,
)

SUPPORTED_FILE_MODES = {"100644", "100755"}
WORKSPACE_GIT_FILE_READ_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class ProjectBlobRecord:
    mode: str
    object_type: str
    object_id: str
    path: str
    payload: bytes


@dataclass(frozen=True)
class GitTreeRecord:
    mode: str
    object_type: str
    object_id: str
    path: str


def git_ls_tree(
    git_top_level: str,
    run_base_commit: str,
    git_top_relative_path: str,
) -> GitTreeRecord | None:
    result = run_git_literal(
        Path(git_top_level),
        "ls-tree",
        "-z",
        "--full-tree",
        "--full-name",
        run_base_commit,
        "--",
        git_top_relative_path,
    )
    records = tuple(item for item in result.stdout.split(b"\0") if item)
    if not records:
        return None
    if len(records) != 1:
        raise ValueError("Git returned multiple tree entries for a literal path.")
    header, separator, path = records[0].partition(b"\t")
    if separator != b"\t":
        raise ValueError("Git returned an invalid tree entry.")
    parts = header.decode("ascii").split(" ")
    if len(parts) != 3:
        raise ValueError("Git returned an invalid tree header.")
    return GitTreeRecord(
        mode=parts[0],
        object_type=parts[1],
        object_id=parts[2],
        path=path.decode("utf-8", errors="surrogateescape"),
    )


def git_cat_blob(git_top_level: str, object_id: str) -> bytes:
    return run_git(Path(git_top_level), "cat-file", "blob", object_id).stdout


def run_git_literal(
    git_top_level: Path,
    *args: str,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            "git",
            *workspace_git_config_args(),
            "--literal-pathspecs",
            "-C",
            git_top_level.as_posix(),
            *args,
        ],
        check=True,
        capture_output=True,
        env=git_env(),
        timeout=WORKSPACE_GIT_FILE_READ_TIMEOUT_SECONDS,
    )


def run_git(
    git_top_level: Path,
    *args: str,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *workspace_git_config_args(), "-C", git_top_level.as_posix(), *args],
        check=True,
        capture_output=True,
        env=git_env(),
        timeout=WORKSPACE_GIT_FILE_READ_TIMEOUT_SECONDS,
    )


def git_env() -> dict[str, str]:
    return sanitized_workspace_git_environment(read_only=True)


def valid_utf8_without_nul(payload: bytes) -> bool:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return "\x00" not in text


def git_error(exc: BaseException) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        stderr = exc.stderr.decode("utf-8", errors="replace").strip()
        return stderr or str(exc)
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"Git command timed out after {exc.timeout} second(s)."
    return str(exc)
