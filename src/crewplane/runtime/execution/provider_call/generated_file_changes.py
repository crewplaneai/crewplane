from __future__ import annotations

import hashlib
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from crewplane.artifacts.generated_files.detection import is_reserved_workspace_path
from crewplane.core.workspace.git_policy import (
    sanitized_workspace_git_environment,
    workspace_git_config_args,
)
from crewplane.runtime.workspace import PreparedWorkspace
from crewplane.runtime.workspace.snapshot import snapshot_entries

GENERATED_FILE_GIT_TIMEOUT_SECONDS = 30.0
FILE_HASH_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class GeneratedFileChangeBaseline:
    invocation_root: Path
    git: _GitGeneratedFileBaseline | None = None
    filesystem_entries: dict[str, str] | None = None

    @classmethod
    def capture(
        cls,
        invocation_root: Path,
        filesystem_fallback_enabled: bool,
    ) -> GeneratedFileChangeBaseline:
        resolved_root = invocation_root.resolve(strict=True)
        git = _GitGeneratedFileBaseline.capture(resolved_root)
        if git is not None and not (
            filesystem_fallback_enabled and git.empty_ignored_pathspec
        ):
            return cls(invocation_root=resolved_root, git=git)
        if filesystem_fallback_enabled:
            return cls(
                invocation_root=resolved_root,
                filesystem_entries=snapshot_entries(resolved_root),
            )
        return cls(invocation_root=resolved_root)

    def candidate_files(self) -> tuple[Path, ...] | None:
        if self.git is not None:
            return self.git.changed_files()
        if self.filesystem_entries is not None:
            current_entries = snapshot_entries(self.invocation_root)
            return _filesystem_changed_files(
                self.invocation_root,
                self.filesystem_entries,
                current_entries,
            )
        return None


@dataclass(frozen=True)
class _GitGeneratedFileBaseline:
    git_top_level: Path
    invocation_root: Path
    pathspec: str
    fingerprints_by_path: dict[str, str | None]
    empty_ignored_pathspec: bool

    @classmethod
    def capture(cls, invocation_root: Path) -> _GitGeneratedFileBaseline | None:
        try:
            git_top_level = _git_top_level(invocation_root)
            pathspec = _git_pathspec(git_top_level, invocation_root)
            paths = _git_status_paths(git_top_level, pathspec)
            empty_ignored_pathspec = not paths and _git_pathspec_is_ignored(
                git_top_level,
                pathspec,
            )
        except (
            OSError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            ValueError,
        ):
            return None
        return cls(
            git_top_level=git_top_level,
            invocation_root=invocation_root,
            pathspec=pathspec,
            fingerprints_by_path={
                path: _regular_file_fingerprint(git_top_level / path) for path in paths
            },
            empty_ignored_pathspec=empty_ignored_pathspec,
        )

    def changed_files(self) -> tuple[Path, ...]:
        paths = _git_status_paths(self.git_top_level, self.pathspec)
        candidates: list[Path] = []
        for path in paths:
            candidate = self.git_top_level / path
            if not _path_is_candidate(candidate, self.invocation_root):
                continue
            current_fingerprint = _regular_file_fingerprint(candidate)
            if current_fingerprint is None:
                continue
            if self.fingerprints_by_path.get(path) == current_fingerprint:
                continue
            candidates.append(candidate)
        return tuple(sorted(candidates))


def changed_generated_file_paths(
    prepared_workspace: PreparedWorkspace,
    workspace_root: Path,
) -> set[str]:
    workspace = prepared_workspace.invocation_context.workspace
    if workspace is None or workspace.checkout_root is None:
        return set()
    checkout_root = workspace.checkout_root
    changed_paths = _changed_checkout_paths(prepared_workspace, checkout_root)
    return _paths_relative_to_workspace_root(
        changed_paths,
        checkout_root,
        workspace_root,
    )


def resolved_real_directory(path: Path, label: str) -> Path:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise RuntimeError(f"{label} is missing: {path.as_posix()}") from exc
    if not stat.S_ISDIR(mode):
        raise RuntimeError(f"{label} is not a real directory: {path.as_posix()}")
    return path.resolve(strict=True)


def _changed_checkout_paths(
    prepared_workspace: PreparedWorkspace,
    checkout_root: Path,
) -> tuple[str, ...]:
    if prepared_workspace.workspace_kind not in {"snapshot", "worktree"}:
        return ()
    current_entries = snapshot_entries(checkout_root)
    initial_entries = prepared_workspace.initial_snapshot_entries or {}
    return tuple(
        sorted(
            path
            for path in set(initial_entries) | set(current_entries)
            if initial_entries.get(path) != current_entries.get(path)
        )
    )


def _paths_relative_to_workspace_root(
    changed_paths: tuple[str, ...],
    checkout_root: Path,
    workspace_root: Path,
) -> set[str]:
    relative_paths: set[str] = set()
    resolved_workspace_root = workspace_root.resolve(strict=True)
    for changed_path in changed_paths:
        candidate = checkout_root.joinpath(*Path(changed_path).parts)
        try:
            relative_path = candidate.resolve(strict=False).relative_to(
                resolved_workspace_root
            )
        except (OSError, ValueError):
            continue
        if not relative_path.parts:
            continue
        relative_paths.add(relative_path.as_posix())
    return relative_paths


def _git_top_level(invocation_root: Path) -> Path:
    top_level = _run_git_text(invocation_root, "rev-parse", "--show-toplevel")
    return Path(top_level).resolve(strict=True)


def _git_pathspec(git_top_level: Path, invocation_root: Path) -> str:
    relative = invocation_root.relative_to(git_top_level)
    return relative.as_posix() if relative.parts else "."


def _git_status_paths(git_top_level: Path, pathspec: str) -> tuple[str, ...]:
    result = _run_git(
        git_top_level,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignored=no",
        "--",
        pathspec,
    )
    records = tuple(record for record in result.stdout.split(b"\0") if record)
    paths: list[str] = []
    index = 0
    while index < len(records):
        record = records[index].decode("utf-8", errors="surrogateescape")
        if len(record) < 4:
            index += 1
            continue
        status = record[:2]
        path = record[3:]
        paths.append(path)
        index += 2 if ("R" in status or "C" in status) else 1
    return tuple(sorted(dict.fromkeys(paths)))


def _git_pathspec_is_ignored(git_top_level: Path, pathspec: str) -> bool:
    result = _run_git_unchecked(
        git_top_level,
        "check-ignore",
        "-q",
        "--",
        pathspec,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise subprocess.CalledProcessError(
        result.returncode,
        result.args,
        output=result.stdout,
        stderr=result.stderr,
    )


def _run_git_text(cwd: Path, *args: str) -> str:
    return _run_git(cwd, *args).stdout.decode("utf-8").strip()


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *workspace_git_config_args(), "-C", cwd.as_posix(), *args],
        check=True,
        capture_output=True,
        env=sanitized_workspace_git_environment(read_only=True),
        timeout=GENERATED_FILE_GIT_TIMEOUT_SECONDS,
    )


def _run_git_unchecked(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *workspace_git_config_args(), "-C", cwd.as_posix(), *args],
        check=False,
        capture_output=True,
        env=sanitized_workspace_git_environment(read_only=True),
        timeout=GENERATED_FILE_GIT_TIMEOUT_SECONDS,
    )


def _filesystem_changed_files(
    root: Path,
    initial_entries: dict[str, str],
    current_entries: dict[str, str],
) -> tuple[Path, ...]:
    candidates: list[Path] = []
    for changed_path in sorted(set(initial_entries) | set(current_entries)):
        if initial_entries.get(changed_path) == current_entries.get(changed_path):
            continue
        candidate = root.joinpath(*Path(changed_path).parts)
        if _path_is_candidate(candidate, root):
            candidates.append(candidate)
    return tuple(candidates)


def _path_is_candidate(candidate: Path, invocation_root: Path) -> bool:
    try:
        relative_path = candidate.resolve(strict=False).relative_to(invocation_root)
    except (OSError, ValueError):
        return False
    if not relative_path.parts or is_reserved_workspace_path(relative_path):
        return False
    try:
        mode = candidate.lstat().st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode) and not stat.S_ISLNK(mode)


def _regular_file_fingerprint(path: Path) -> str | None:
    try:
        mode = path.lstat().st_mode
    except OSError:
        return None
    if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
        return None
    digest = hashlib.sha256()
    stat_result = path.stat()
    digest.update(str(stat_result.st_size).encode("ascii"))
    digest.update(b"\0")
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(FILE_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()
