from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from crewplane.core.workspace.git_policy import workspace_git_config_args
from crewplane.core.workspace.policy import WORKTREE_CONTRACT_MODES

from .source_types import WorkspacePolicyBuilder

GIT_MIN_VERSION = (2, 34, 1)
GIT_SOURCE_PROBE_TIMEOUT_SECONDS = 30.0
GIT_CAPABILITY_PROBES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "object-format discovery",
        ("rev-parse", "--show-object-format=storage"),
    ),
    (
        "porcelain v2 status",
        ("status", "--porcelain=v2", "-z", "--untracked-files=no"),
    ),
    (
        "index flag inspection",
        ("ls-files", "-v", "-z"),
    ),
    (
        "full-tree listing",
        ("ls-tree", "-r", "-z", "--full-tree", "--full-name", "HEAD"),
    ),
    (
        "object existence checks",
        ("cat-file", "-e", "HEAD^{tree}"),
    ),
    (
        "effective attribute inspection",
        (
            "--literal-pathspecs",
            "check-attr",
            "--cached",
            "-z",
            "--all",
            "--",
            "__crewplane_missing_probe_path__",
        ),
    ),
)
GIT_ENV_UNSET = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_ATTR_NOSYSTEM",
    "GIT_ATTR_SOURCE",
    "GIT_LITERAL_PATHSPECS",
    "GIT_GLOB_PATHSPECS",
    "GIT_NOGLOB_PATHSPECS",
    "GIT_ICASE_PATHSPECS",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
)
GIT_ENV_PREFIXES_UNSET = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
GIT_ENV_TEMPLATE: Mapping[str, str] = {
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_NO_LAZY_FETCH": "1",
    "GIT_TERMINAL_PROMPT": "0",
}


@dataclass(frozen=True)
class GitSourceContext:
    run_base_commit: str
    source_tree: str
    object_format: str
    git_top_level: Path
    project_root_relative_path: str
    active_git_dir: Path
    common_git_dir: Path
    git_version: str


def discover_git_context(
    project_root: Path,
    builder: WorkspacePolicyBuilder,
) -> GitSourceContext | None:
    try:
        top_level = Path(git_text(project_root, "rev-parse", "--show-toplevel"))
        project_relative = project_root.resolve(strict=False).relative_to(
            top_level.resolve(strict=False)
        )
        common_dir = resolve_git_path(
            project_root,
            git_text(project_root, "rev-parse", "--git-common-dir"),
        )
        return GitSourceContext(
            run_base_commit=git_text(project_root, "rev-parse", "HEAD^{commit}"),
            source_tree=git_text(project_root, "rev-parse", "HEAD^{tree}"),
            object_format=git_text(
                project_root,
                "rev-parse",
                "--show-object-format=storage",
            ),
            git_top_level=top_level.resolve(strict=False),
            project_root_relative_path=project_relative.as_posix() or ".",
            active_git_dir=Path(
                git_text(project_root, "rev-parse", "--absolute-git-dir")
            ).resolve(strict=False),
            common_git_dir=common_dir.resolve(strict=False),
            git_version=git_version(project_root),
        )
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        ValueError,
    ) as exc:
        builder.errors.append(
            "Workspace source policy failed: workspace-enabled mode requires a "
            "Git repository with a valid HEAD commit. No usable repository was "
            f"found at {project_root.as_posix()} ({git_error(exc)}). Initialize "
            "and commit the project, run from a Git checkout, or set "
            "settings.workspace.enabled: false to run without workspace isolation."
        )
        return None


def validate_git_version(
    version_text: str,
    builder: WorkspacePolicyBuilder,
) -> None:
    version = parse_git_version(version_text)
    if version is not None and version >= GIT_MIN_VERSION:
        return
    builder.errors.append(
        "Workspace Git contract failed: Git 2.34.1 or newer is required for "
        f"{WORKTREE_CONTRACT_MODES[0]}; found '{version_text}'."
    )


def validate_git_capabilities(
    git_context: GitSourceContext,
    builder: WorkspacePolicyBuilder,
) -> None:
    failures = [
        name
        for name, args in GIT_CAPABILITY_PROBES
        if not git_probe_supported(git_context.git_top_level, *args)
    ]
    if not git_literal_pathspec_probe_supported(git_context.git_top_level):
        failures.append("literal pathspec tree lookup")
    if not worktree_locking_supported(git_context.git_top_level):
        failures.append("locked detached worktree provisioning")
    if failures:
        builder.errors.append(
            "Workspace Git contract failed: required Git command-surface probes "
            f"failed for {WORKTREE_CONTRACT_MODES[0]}: {', '.join(failures)}."
        )


def repository_id(git_context: GitSourceContext, project_root: Path) -> str:
    payload = "|".join(
        [
            git_context.common_git_dir.as_posix(),
            project_root.resolve(strict=False).as_posix(),
            git_context.object_format,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def git_text(project_root: Path, *args: str) -> str:
    return run_git(project_root, *args).stdout.decode("utf-8").strip()


def git_zero_records(project_root: Path, *args: str) -> tuple[str, ...]:
    output = run_git(project_root, *args).stdout.decode("utf-8", errors="replace")
    return tuple(record for record in output.split("\0") if record)


def run_git(project_root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return run_git_with_env(project_root, args, {})


def git_zero_records_with_env(
    project_root: Path,
    args: tuple[str, ...],
    env_overrides: Mapping[str, str],
) -> tuple[str, ...]:
    output = run_git_with_env(
        project_root,
        args,
        env_overrides,
    ).stdout.decode("utf-8", errors="replace")
    return tuple(record for record in output.split("\0") if record)


def run_git_with_env(
    project_root: Path,
    args: tuple[str, ...],
    env_overrides: Mapping[str, str],
) -> subprocess.CompletedProcess[bytes]:
    env = git_env()
    env.update(env_overrides)
    return subprocess.run(
        git_command(project_root, args),
        check=True,
        capture_output=True,
        env=env,
        timeout=GIT_SOURCE_PROBE_TIMEOUT_SECONDS,
    )


def run_git_unchecked(
    project_root: Path,
    *args: str,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        git_command(project_root, args),
        check=False,
        capture_output=True,
        env=git_env(),
        timeout=GIT_SOURCE_PROBE_TIMEOUT_SECONDS,
    )


def git_command(project_root: Path, args: tuple[str, ...]) -> list[str]:
    return [
        "git",
        *workspace_git_config_args(),
        "--no-optional-locks",
        "-C",
        project_root.as_posix(),
        *args,
    ]


def git_probe_supported(project_root: Path, *args: str) -> bool:
    try:
        run_git(project_root, *args)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return True


def git_literal_pathspec_probe_supported(project_root: Path) -> bool:
    try:
        run_git_with_env(
            project_root,
            (
                "--literal-pathspecs",
                "ls-tree",
                "-z",
                "--full-tree",
                "--full-name",
                "HEAD",
                "--",
                "__crewplane_missing_probe_path__",
            ),
            {},
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return True


def worktree_locking_supported(project_root: Path) -> bool:
    try:
        add_help = worktree_help_text(project_root, "add")
        if "--detach" not in add_help:
            return False
        if "--lock" in add_help and "--reason" in add_help:
            return True
        lock_help = worktree_help_text(project_root, "lock")
        return "--reason" in lock_help
    except subprocess.TimeoutExpired:
        return False


def worktree_help_text(project_root: Path, subcommand: str) -> str:
    result = run_git_unchecked(project_root, "worktree", subcommand, "-h")
    return "\n".join(
        (
            result.stdout.decode("utf-8", errors="replace"),
            result.stderr.decode("utf-8", errors="replace"),
        )
    )


def git_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in GIT_ENV_UNSET:
        env.pop(key, None)
    for key in tuple(env):
        if key.startswith(GIT_ENV_PREFIXES_UNSET):
            env.pop(key, None)
    env.update(GIT_ENV_TEMPLATE)
    return env


def git_version(project_root: Path) -> str:
    output = run_git(project_root, "--version").stdout.decode("utf-8").strip()
    return output.removeprefix("git version ").strip()


def parse_git_version(version_text: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", version_text)
    if match is None:
        return None
    patch = int(match.group(3)) if match.group(3) is not None else 0
    return int(match.group(1)), int(match.group(2)), patch


def git_config_value(project_root: Path, key: str) -> str | None:
    try:
        return git_text(project_root, "config", "--local", "--get", key)
    except subprocess.CalledProcessError:
        return None


def git_config_bool(project_root: Path, key: str) -> bool | None:
    try:
        value = git_text(project_root, "config", "--local", "--type=bool", "--get", key)
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 1:
            return None
        raise
    return value == "true"


def git_path_exists(project_root: Path, rev_path: str) -> bool:
    try:
        run_git(project_root, "cat-file", "-e", rev_path)
    except subprocess.CalledProcessError:
        return False
    return True


def resolve_git_path(top_level: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return top_level / path


def git_error(exc: BaseException) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        stderr = exc.stderr.decode("utf-8", errors="replace").strip()
        return stderr or str(exc)
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"Git command timed out after {exc.timeout} second(s)"
    return str(exc)
