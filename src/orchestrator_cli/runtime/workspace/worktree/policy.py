from __future__ import annotations

import subprocess
from pathlib import Path

from orchestrator_cli.core.workspace_git_policy import (
    config_keys_from_scoped_records,
    effective_policy_lines,
    is_rejected_config_key,
    normalize_config_key,
)

from ..git import git, git_error


def active_git_dir(checkout_root: Path) -> Path:
    value = git(checkout_root).text("rev-parse", "--absolute-git-dir")
    return Path(value).resolve(strict=False)


def reject_worktree_git_policy_drift(checkout_root: Path) -> None:
    if git(checkout_root).text("branch", "--show-current"):
        raise RuntimeError("Workspace worktree must remain detached from branches.")
    git_dir = active_git_dir(checkout_root)
    config_worktree = git_dir / "config.worktree"
    if config_worktree.exists() and config_worktree.stat().st_size > 0:
        raise RuntimeError("Workspace worktree-specific Git config is unsupported.")
    attributes = git_dir / "info" / "attributes"
    if attributes.exists() and attributes.read_text(encoding="utf-8").strip():
        raise RuntimeError("Workspace worktree info/attributes must stay empty.")
    exclude = git_dir / "info" / "exclude"
    if exclude.exists() and effective_policy_lines(exclude):
        raise RuntimeError("Workspace worktree info/exclude must stay empty.")


def reject_common_git_policy_drift(repo_root: Path, common_git_dir: Path) -> None:
    rejected = common_git_config_rejected_keys(repo_root)
    if rejected:
        raise RuntimeError(
            "Workspace common Git config contains newly unsupported keys: "
            f"{', '.join(rejected)}."
        )
    attributes = common_git_dir / "info" / "attributes"
    if (
        attributes.exists()
        and attributes.read_text(encoding="utf-8", errors="ignore").strip()
    ):
        raise RuntimeError("Workspace common Git info/attributes must stay empty.")
    exclude = common_git_dir / "info" / "exclude"
    if exclude.exists() and effective_policy_lines(exclude):
        raise RuntimeError("Workspace common Git info/exclude must stay empty.")
    for path_name, label in (
        ("objects/info/alternates", "object alternates"),
        ("info/grafts", "grafts"),
    ):
        path = common_git_dir / path_name
        if path.exists() and path.read_text(encoding="utf-8", errors="ignore").strip():
            raise RuntimeError(f"Workspace common Git {label} must stay empty.")
    if replacement_refs_exist(common_git_dir):
        raise RuntimeError("Workspace common Git replacement refs must not exist.")


def common_git_config_rejected_keys(repo_root: Path) -> tuple[str, ...]:
    try:
        config_records = git(repo_root).zero_records(
            "config",
            "--local",
            "--no-includes",
            "--list",
            "--name-only",
            "--show-origin",
            "--show-scope",
            "-z",
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"Workspace common Git config could not be inspected: {git_error(exc)}"
        ) from exc
    try:
        scoped_keys = config_keys_from_scoped_records(config_records)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    return tuple(
        sorted(
            key
            for key in (normalize_config_key(record) for record in scoped_keys)
            if is_rejected_config_key(key)
        )
    )


def replacement_refs_exist(common_git_dir: Path) -> bool:
    replace_root = common_git_dir / "refs" / "replace"
    if replace_root.exists() and any(
        path.is_file() for path in replace_root.rglob("*")
    ):
        return True
    packed_refs = common_git_dir / "packed-refs"
    if not packed_refs.exists():
        return False
    return any(
        " refs/replace/" in line
        for line in packed_refs.read_text(
            encoding="utf-8",
            errors="ignore",
        ).splitlines()
    )
