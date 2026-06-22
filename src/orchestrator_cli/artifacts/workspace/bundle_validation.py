from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from orchestrator_cli.core.workspace_git_policy import workspace_git_config_args

from .git_blob_hash import git_stdout_sha256

GIT_BUNDLE_VALIDATION_TIMEOUT_SECONDS = 30.0
_SUPPORTED_OBJECT_FORMATS = frozenset({"sha1", "sha256"})
_GIT_ENV_UNSET = (
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


def workspace_bundle_contains_result(
    git_top_level: str,
    bundle_path: Path,
    result_ref: str,
    result_commit: str,
    object_format: str = "sha1",
) -> bool:
    repo_root = Path(git_top_level)
    if not repo_root.is_dir():
        return False
    try:
        _run_git(repo_root, "bundle", "verify", bundle_path.as_posix())
        listed = _run_git(
            repo_root,
            "bundle",
            "list-heads",
            bundle_path.as_posix(),
            result_ref,
        )
        return _listed_head_matches(
            listed.stdout,
            result_ref,
            result_commit,
        ) and _bundle_result_is_commit(bundle_path, result_commit, object_format)
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        UnicodeDecodeError,
        ValueError,
    ):
        return False


def workspace_bundle_contains_result_tree(
    git_top_level: str,
    bundle_path: Path,
    result_ref: str,
    result_commit: str,
    result_tree: str,
    object_format: str,
) -> bool:
    repo_root = Path(git_top_level)
    if not repo_root.is_dir():
        return False
    try:
        _run_git(repo_root, "bundle", "verify", bundle_path.as_posix())
        listed = _run_git(
            repo_root,
            "bundle",
            "list-heads",
            bundle_path.as_posix(),
            result_ref,
        )
        return _listed_head_matches(
            listed.stdout,
            result_ref,
            result_commit,
        ) and _bundle_result_tree_matches(
            bundle_path,
            result_commit,
            result_tree,
            object_format,
        )
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        UnicodeDecodeError,
        ValueError,
    ):
        return False


def workspace_blob_descriptor_matches(
    git_top_level: str,
    source_commit: str,
    source_tree: str,
    git_path: str,
    git_blob: str,
    git_file_mode: str,
    byte_size: int,
    canonical_sha256: str,
    object_format: str,
    bundle_path: Path | None = None,
    bundle_ref: str | None = None,
) -> bool:
    repo_root = Path(git_top_level)
    if not repo_root.is_dir():
        return False
    try:
        if bundle_path is None:
            return _repo_blob_descriptor_matches(
                repo_root,
                source_commit,
                source_tree,
                git_path,
                git_blob,
                git_file_mode,
                byte_size,
                canonical_sha256,
            )
        _run_git(repo_root, "bundle", "verify", bundle_path.as_posix())
        if bundle_ref is not None:
            listed = _run_git(
                repo_root,
                "bundle",
                "list-heads",
                bundle_path.as_posix(),
                bundle_ref,
            )
            if not _listed_head_matches(listed.stdout, bundle_ref, source_commit):
                return False
        return _bundle_blob_descriptor_matches(
            bundle_path,
            source_commit,
            source_tree,
            git_path,
            git_blob,
            git_file_mode,
            byte_size,
            canonical_sha256,
            object_format,
        )
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        UnicodeDecodeError,
        ValueError,
    ):
        return False


def _run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return _run_git_with_env(repo_root, _sanitized_git_env(), *args)


def _run_git_with_env(
    repo_root: Path,
    env: dict[str, str],
    *args: str,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            "git",
            *workspace_git_config_args(),
            "--no-optional-locks",
            "-C",
            repo_root.as_posix(),
            *args,
        ],
        check=True,
        capture_output=True,
        env=env,
        timeout=GIT_BUNDLE_VALIDATION_TIMEOUT_SECONDS,
    )


def _listed_head_matches(
    output: bytes,
    result_ref: str,
    result_commit: str,
) -> bool:
    lines = output.decode("utf-8").splitlines()
    if len(lines) != 1:
        return False
    object_id, separator, ref_name = lines[0].partition(" ")
    return separator == " " and object_id == result_commit and ref_name == result_ref


def _bundle_result_is_commit(
    bundle_path: Path,
    result_commit: str,
    object_format: str,
) -> bool:
    with tempfile.TemporaryDirectory() as temp_dir:
        git_dir = Path(temp_dir) / "bundle.git"
        env = _sanitized_git_env()
        _init_isolated_bare_repo(git_dir, env, object_format)
        _run_git_dir(git_dir, env, "bundle", "unbundle", bundle_path.as_posix())
        object_type = _run_git_dir(
            git_dir,
            env,
            "cat-file",
            "-t",
            result_commit,
        ).stdout.decode("utf-8")
    return object_type.strip() == "commit"


def _bundle_result_tree_matches(
    bundle_path: Path,
    result_commit: str,
    result_tree: str,
    object_format: str,
) -> bool:
    with tempfile.TemporaryDirectory() as temp_dir:
        git_dir = Path(temp_dir) / "bundle.git"
        env = _sanitized_git_env()
        _init_isolated_bare_repo(git_dir, env, object_format)
        _run_git_dir(git_dir, env, "bundle", "unbundle", bundle_path.as_posix())
        object_type = _run_git_dir(
            git_dir,
            env,
            "cat-file",
            "-t",
            result_commit,
        ).stdout.decode("utf-8")
        if object_type.strip() != "commit":
            return False
        actual_tree = _run_git_dir(
            git_dir,
            env,
            "rev-parse",
            f"{result_commit}^{{tree}}",
        ).stdout.decode("utf-8")
    return actual_tree.strip() == result_tree


def _bundle_blob_descriptor_matches(
    bundle_path: Path,
    source_commit: str,
    source_tree: str,
    git_path: str,
    git_blob: str,
    git_file_mode: str,
    byte_size: int,
    canonical_sha256: str,
    object_format: str,
) -> bool:
    with tempfile.TemporaryDirectory() as temp_dir:
        git_dir = Path(temp_dir) / "bundle.git"
        env = _sanitized_git_env()
        _init_isolated_bare_repo(git_dir, env, object_format)
        _run_git_dir(git_dir, env, "bundle", "unbundle", bundle_path.as_posix())
        return _git_dir_blob_descriptor_matches(
            git_dir,
            env,
            source_commit,
            source_tree,
            git_path,
            git_blob,
            git_file_mode,
            byte_size,
            canonical_sha256,
        )


def _repo_blob_descriptor_matches(
    repo_root: Path,
    source_commit: str,
    source_tree: str,
    git_path: str,
    git_blob: str,
    git_file_mode: str,
    byte_size: int,
    canonical_sha256: str,
) -> bool:
    env = _sanitized_git_env()
    actual_tree = _run_git_with_env(
        repo_root,
        env,
        "rev-parse",
        f"{source_commit}^{{tree}}",
    ).stdout.decode("utf-8")
    if actual_tree.strip() != source_tree:
        return False
    entry = _tree_blob_entry(
        _run_git_with_env(
            repo_root,
            env,
            "--literal-pathspecs",
            "ls-tree",
            "-z",
            source_commit,
            "--",
            git_path,
        ).stdout,
        git_path,
    )
    if entry is None:
        return False
    mode, object_id = entry
    return (
        mode == git_file_mode
        and object_id == git_blob
        and _repo_blob_size(repo_root, env, object_id) == byte_size
        and _repo_blob_sha256(repo_root, env, object_id) == canonical_sha256
    )


def _git_dir_blob_descriptor_matches(
    git_dir: Path,
    env: dict[str, str],
    source_commit: str,
    source_tree: str,
    git_path: str,
    git_blob: str,
    git_file_mode: str,
    byte_size: int,
    canonical_sha256: str,
) -> bool:
    actual_tree = _run_git_dir(
        git_dir,
        env,
        "rev-parse",
        f"{source_commit}^{{tree}}",
    ).stdout.decode("utf-8")
    if actual_tree.strip() != source_tree:
        return False
    entry = _tree_blob_entry(
        _run_git_dir(
            git_dir,
            env,
            "--literal-pathspecs",
            "ls-tree",
            "-z",
            source_commit,
            "--",
            git_path,
        ).stdout,
        git_path,
    )
    if entry is None:
        return False
    mode, object_id = entry
    return (
        mode == git_file_mode
        and object_id == git_blob
        and _git_dir_blob_size(git_dir, env, object_id) == byte_size
        and _git_dir_blob_sha256(git_dir, env, object_id) == canonical_sha256
    )


def _tree_blob_entry(output: bytes, expected_path: str) -> tuple[str, str] | None:
    records = [record for record in output.decode("utf-8").split("\0") if record]
    if len(records) != 1:
        return None
    header, separator, path = records[0].partition("\t")
    if separator != "\t" or path != expected_path:
        return None
    parts = header.split(" ")
    if len(parts) != 3 or parts[1] != "blob":
        return None
    return parts[0], parts[2]


def _repo_blob_size(repo_root: Path, env: dict[str, str], object_id: str) -> int:
    return int(
        _run_git_with_env(repo_root, env, "cat-file", "-s", object_id)
        .stdout.decode("utf-8")
        .strip()
    )


def _git_dir_blob_size(git_dir: Path, env: dict[str, str], object_id: str) -> int:
    return int(
        _run_git_dir(git_dir, env, "cat-file", "-s", object_id)
        .stdout.decode("utf-8")
        .strip()
    )


def _repo_blob_sha256(repo_root: Path, env: dict[str, str], object_id: str) -> str:
    return git_stdout_sha256(
        ["git", *workspace_git_config_args(), "-C", repo_root.as_posix()],
        env,
        object_id,
        GIT_BUNDLE_VALIDATION_TIMEOUT_SECONDS,
    )


def _git_dir_blob_sha256(git_dir: Path, env: dict[str, str], object_id: str) -> str:
    return git_stdout_sha256(
        ["git", *workspace_git_config_args(), f"--git-dir={git_dir.as_posix()}"],
        env,
        object_id,
        GIT_BUNDLE_VALIDATION_TIMEOUT_SECONDS,
    )


def _init_isolated_bare_repo(
    git_dir: Path,
    env: dict[str, str],
    object_format: str = "sha1",
) -> None:
    if object_format not in _SUPPORTED_OBJECT_FORMATS:
        raise ValueError(f"Unsupported Git object format: {object_format}")
    empty_template_dir = git_dir.parent / "empty-template"
    empty_template_dir.mkdir()
    subprocess.run(
        [
            "git",
            "init",
            "--bare",
            f"--object-format={object_format}",
            f"--template={empty_template_dir.as_posix()}",
            git_dir.as_posix(),
        ],
        check=True,
        capture_output=True,
        env=env,
        timeout=GIT_BUNDLE_VALIDATION_TIMEOUT_SECONDS,
    )


def _run_git_dir(
    git_dir: Path,
    env: dict[str, str],
    *args: str,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            "git",
            *workspace_git_config_args(),
            "--no-optional-locks",
            f"--git-dir={git_dir.as_posix()}",
            *args,
        ],
        check=True,
        capture_output=True,
        env=env,
        timeout=GIT_BUNDLE_VALIDATION_TIMEOUT_SECONDS,
    )


def _sanitized_git_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in _GIT_ENV_UNSET:
        env.pop(key, None)
    for key in tuple(env):
        if key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            env.pop(key, None)
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env
