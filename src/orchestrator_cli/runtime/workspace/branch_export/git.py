from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Literal

from orchestrator_cli.core.preflight.models import WorkspaceSourceSnapshot
from orchestrator_cli.runtime.workspace.git import GitCommand, git, git_error
from orchestrator_cli.runtime.workspace.locks import git_metadata_lock
from orchestrator_cli.runtime.workspace.worktree.refs import (
    checked_ref,
    safe_ref_component,
)


def ensure_result_commit_available(
    source: WorkspaceSourceSnapshot,
    bundle_path: Path,
    result_ref: str,
    result_commit: str,
) -> None:
    if commit_exists(source, result_commit):
        return
    import_ref = checked_ref(
        Path(source.git_top_level),
        f"refs/orchestrator-cli/exported/{safe_ref_component(result_commit[:24])}",
    )
    with git_metadata_lock(Path(source.common_git_dir)):
        command = git(Path(source.git_top_level))
        command.run("bundle", "verify", bundle_path.as_posix())
        command.run("fetch", bundle_path.as_posix(), f"{result_ref}:{import_ref}")
    if not commit_exists(source, result_commit):
        raise RuntimeError("Workspace branch export bundle import failed.")


def commit_exists(source: WorkspaceSourceSnapshot, commit: str) -> bool:
    try:
        git(Path(source.git_top_level)).run("cat-file", "-e", f"{commit}^{{commit}}")
    except subprocess.CalledProcessError:
        return False
    return True


def validated_branch_ref(
    source: WorkspaceSourceSnapshot,
    branch_name: str,
) -> str:
    if branch_name.startswith("refs/"):
        raise RuntimeError("Branch export branch_name must not include refs/.")
    branch_ref = f"refs/heads/{branch_name}"
    try:
        normalized = git(Path(source.git_top_level)).text(
            "check-ref-format",
            "--normalize",
            branch_ref,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"Invalid branch export branch name '{branch_name}': {git_error(exc)}"
        ) from exc
    if normalized != branch_ref:
        raise RuntimeError(f"Unsafe branch export branch name '{branch_name}'.")
    return normalized


BranchExportOperation = Literal[
    "created",
    "verified_existing",
    "skipped",
    "failed_verification",
]


def create_or_verify_branch_ref(
    source: WorkspaceSourceSnapshot,
    branch_ref: str,
    result_commit: str,
    allow_existing: bool = False,
) -> BranchExportOperation:
    repo_root = Path(source.git_top_level)
    with git_metadata_lock(Path(source.common_git_dir)):
        command = git(repo_root)
        current_commit = branch_commit(command, branch_ref)
        if current_commit is not None:
            if allow_existing and current_commit == result_commit:
                return "verified_existing"
            raise RuntimeError(
                "Workspace branch export refuses to overwrite existing branch "
                f"'{branch_ref.removeprefix('refs/heads/')}'."
            )
        try:
            command.run("update-ref", branch_ref, result_commit, "")
        except subprocess.CalledProcessError as exc:
            raced_commit = branch_commit(command, branch_ref)
            if allow_existing and raced_commit == result_commit:
                return "verified_existing"
            if raced_commit is not None:
                raise RuntimeError(
                    "Workspace branch export refuses to overwrite existing branch "
                    f"'{branch_ref.removeprefix('refs/heads/')}'."
                ) from exc
            raise
    return "created"


def branch_ref_exists(source: WorkspaceSourceSnapshot, branch_ref: str) -> bool:
    return branch_ref_commit(source, branch_ref) is not None


def branch_ref_commit(
    source: WorkspaceSourceSnapshot,
    branch_ref: str,
) -> str | None:
    return branch_commit(git(Path(source.git_top_level)), branch_ref)


def planned_branch_operation(
    source: WorkspaceSourceSnapshot,
    branch_ref: str,
    result_commit: str,
) -> tuple[BranchExportOperation, str | None]:
    current_commit = branch_ref_commit(source, branch_ref)
    if current_commit is None:
        return "created", None
    if current_commit == result_commit:
        return "verified_existing", None
    return (
        "failed_verification",
        "Workspace branch export refuses to overwrite existing branch "
        f"'{branch_ref.removeprefix('refs/heads/')}'.",
    )


def branch_commit(command: GitCommand, branch_ref: str) -> str | None:
    try:
        command.run("show-ref", "--verify", "--quiet", branch_ref)
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 1:
            return None
        raise
    return command.text("rev-parse", "--verify", f"{branch_ref}^{{commit}}")
