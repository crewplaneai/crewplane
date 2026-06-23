from __future__ import annotations

import os
from pathlib import Path

WORKSPACE_GIT_CONFIG_OVERLAY = (
    ("core.filemode", "true"),
    ("core.symlinks", "true"),
    ("core.ignorecase", "false"),
    ("core.precomposeunicode", "false"),
    ("core.autocrlf", "false"),
    ("core.eol", "lf"),
    ("core.safecrlf", "false"),
    ("core.attributesfile", os.devnull),
    ("core.excludesfile", os.devnull),
    ("core.protectHFS", "true"),
    ("core.protectNTFS", "true"),
    ("commit.gpgsign", "false"),
    ("tag.gpgsign", "false"),
)
WORKSPACE_GIT_ENV_UNSET = (
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
    "GIT_TEMPLATE_DIR",
    "GIT_ATTR_NOSYSTEM",
    "GIT_ATTR_SOURCE",
    "GIT_LITERAL_PATHSPECS",
    "GIT_GLOB_PATHSPECS",
    "GIT_NOGLOB_PATHSPECS",
    "GIT_ICASE_PATHSPECS",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
)
WORKSPACE_GIT_ENV_PREFIXES_UNSET = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
WORKSPACE_GIT_BASE_ENVIRONMENT = (
    ("GIT_CONFIG_NOSYSTEM", "1"),
    ("GIT_CONFIG_GLOBAL", os.devnull),
    ("GIT_ATTR_NOSYSTEM", "1"),
    ("GIT_NO_REPLACE_OBJECTS", "1"),
    ("GIT_NO_LAZY_FETCH", "1"),
    ("GIT_TERMINAL_PROMPT", "0"),
)
# Git commit object IDs include author and committer dates. These synthetic
# lineage commits stay reproducible; actual capture time lives in artifacts.
WORKSPACE_GIT_DETERMINISTIC_COMMIT_ENVIRONMENT = (
    ("GIT_AUTHOR_NAME", "crewplane"),
    ("GIT_AUTHOR_EMAIL", "crewplane@example.invalid"),
    ("GIT_AUTHOR_DATE", "2000-01-01T00:00:00+0000"),
    ("GIT_COMMITTER_NAME", "crewplane"),
    ("GIT_COMMITTER_EMAIL", "crewplane@example.invalid"),
    ("GIT_COMMITTER_DATE", "2000-01-01T00:00:00+0000"),
)
OVERRIDDEN_CONFIG_KEYS = frozenset(
    key.lower() for key, _ in WORKSPACE_GIT_CONFIG_OVERLAY
) - frozenset(
    {
        "core.attributesfile",
        "core.excludesfile",
    }
)
REJECTED_CONFIG_PREFIXES = (
    "include.",
    "includeif.",
    "filter.",
    "index.",
)
REJECTED_REMOTE_CONFIG_SUFFIXES = (
    ".partialclonefilter",
    ".promisor",
)
REJECTED_CONFIG_KEYS = {
    "core.attributesfile",
    "core.excludesfile",
    "core.worktree",
    "core.fsmonitor",
    "core.untrackedcache",
    "core.splitindex",
    "extensions.worktreeconfig",
}


def normalize_config_key(value: str) -> str:
    return value.strip().lower()


def is_rejected_config_key(key: str) -> bool:
    return (
        key in REJECTED_CONFIG_KEYS
        or any(key.startswith(prefix) for prefix in REJECTED_CONFIG_PREFIXES)
        or _is_rejected_remote_config_key(key)
        or _is_rejected_extension_config_key(key)
    )


def workspace_git_config_args() -> tuple[str, ...]:
    args: list[str] = []
    for key, value in WORKSPACE_GIT_CONFIG_OVERLAY:
        args.extend(("-c", f"{key}={value}"))
    return tuple(args)


def workspace_git_config_environment() -> dict[str, str]:
    env = {"GIT_CONFIG_COUNT": str(len(WORKSPACE_GIT_CONFIG_OVERLAY))}
    for index, (key, value) in enumerate(WORKSPACE_GIT_CONFIG_OVERLAY):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def dynamic_workspace_git_environment_keys(
    environ: dict[str, str] | None = None,
) -> tuple[str, ...]:
    source = os.environ if environ is None else environ
    return tuple(
        key for key in source if key.startswith(WORKSPACE_GIT_ENV_PREFIXES_UNSET)
    )


def workspace_git_environment_unset_keys(
    environ: dict[str, str] | None = None,
) -> tuple[str, ...]:
    return (
        *WORKSPACE_GIT_ENV_UNSET,
        *dynamic_workspace_git_environment_keys(environ),
    )


def workspace_git_base_environment(
    read_only: bool = False,
    ceiling_directories: Path | None = None,
    include_config_overlay: bool = False,
) -> dict[str, str]:
    env = dict(WORKSPACE_GIT_BASE_ENVIRONMENT)
    if read_only:
        env["GIT_OPTIONAL_LOCKS"] = "0"
    if ceiling_directories is not None:
        env["GIT_CEILING_DIRECTORIES"] = ceiling_directories.as_posix()
    if include_config_overlay:
        env.update(workspace_git_config_environment())
    return env


def sanitized_workspace_git_environment(
    index_path: Path | None = None,
    read_only: bool = True,
    ceiling_directories: Path | None = None,
) -> dict[str, str]:
    env = dict(os.environ)
    for key in workspace_git_environment_unset_keys(env):
        env.pop(key, None)
    env.update(
        workspace_git_base_environment(
            read_only=read_only,
            ceiling_directories=ceiling_directories,
        )
    )
    if index_path is not None:
        env["GIT_INDEX_FILE"] = index_path.as_posix()
    return env


def deterministic_workspace_commit_environment(
    index_path: Path | None = None,
) -> dict[str, str]:
    env = sanitized_workspace_git_environment(index_path)
    env.update(WORKSPACE_GIT_DETERMINISTIC_COMMIT_ENVIRONMENT)
    return env


def local_config_policy_summary(keys: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    normalized = sorted({normalize_config_key(key) for key in keys})
    rejected = tuple(key for key in normalized if is_rejected_config_key(key))
    overridden = tuple(
        key
        for key in normalized
        if key not in rejected and key in OVERRIDDEN_CONFIG_KEYS
    )
    ignored_neutral = tuple(
        key for key in normalized if key not in rejected and key not in overridden
    )
    return {
        "rejected": rejected,
        "overridden": overridden,
        "ignored_neutral": ignored_neutral,
    }


def config_keys_from_scoped_records(records: tuple[str, ...]) -> tuple[str, ...]:
    if len(records) % 3 != 0:
        raise ValueError("Git returned an invalid scoped config record stream.")
    return tuple(records[index] for index in range(2, len(records), 3))


def effective_policy_lines(path: Path) -> tuple[str, ...]:
    return tuple(
        stripped
        for stripped in (
            line.strip()
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
        )
        if stripped and not stripped.startswith("#")
    )


def _is_rejected_remote_config_key(key: str) -> bool:
    return key.startswith("remote.") and key.endswith(REJECTED_REMOTE_CONFIG_SUFFIXES)


def _is_rejected_extension_config_key(key: str) -> bool:
    return key.startswith("extensions.") and key != "extensions.objectformat"
