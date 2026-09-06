from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Literal

from crewplane.core.preflight.models import WorkspaceSourceSnapshot
from crewplane.runtime.workspace.git import GitCommand, git, git_error
from crewplane.runtime.workspace.locks import git_metadata_lock


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


class BranchExportOperationCompletedError(RuntimeError):
    pass


class _BranchRefUpdateOutcomeAmbiguousError(RuntimeError):
    pass


class _SymbolicBranchRefError(RuntimeError):
    pass


def create_or_verify_branch_ref(
    source: WorkspaceSourceSnapshot,
    branch_ref: str,
    result_commit: str,
    allow_existing: bool = False,
    allow_create: bool = True,
) -> BranchExportOperation:
    repo_root = Path(source.git_top_level)
    completed_operation: BranchExportOperation | None = None
    operation_outcome_ambiguous = False
    try:
        with git_metadata_lock(Path(source.common_git_dir)):
            command = git(repo_root)
            try:
                completed_operation = _create_or_verify_locked_branch_ref(
                    command,
                    branch_ref,
                    result_commit,
                    allow_existing,
                    allow_create,
                )
            except _BranchRefUpdateOutcomeAmbiguousError:
                operation_outcome_ambiguous = True
                raise
    except Exception as exc:
        if completed_operation is not None or operation_outcome_ambiguous:
            raise BranchExportOperationCompletedError(str(exc)) from exc
        raise
    if completed_operation is None:
        raise RuntimeError("Workspace branch export operation did not complete.")
    return completed_operation


def _create_or_verify_locked_branch_ref(
    command: GitCommand,
    branch_ref: str,
    result_commit: str,
    allow_existing: bool,
    allow_create: bool,
) -> BranchExportOperation:
    current_commit = branch_commit(command, branch_ref)
    if current_commit is not None:
        if allow_existing and current_commit == result_commit:
            return "verified_existing"
        raise RuntimeError(_existing_branch_message(branch_ref))
    if not allow_create:
        raise RuntimeError(
            "Workspace branch export destination branch disappeared before "
            "locked verification."
        )
    return _create_missing_branch_ref(
        command,
        branch_ref,
        result_commit,
        allow_existing,
    )


def _create_missing_branch_ref(
    command: GitCommand,
    branch_ref: str,
    result_commit: str,
    allow_existing: bool,
) -> BranchExportOperation:
    try:
        command.run("update-ref", "--no-deref", branch_ref, result_commit, "")
    except subprocess.TimeoutExpired as exc:
        raise _BranchRefUpdateOutcomeAmbiguousError(str(exc)) from exc
    except subprocess.CalledProcessError as exc:
        raced_commit = branch_commit(command, branch_ref)
        if allow_existing and raced_commit == result_commit:
            return "verified_existing"
        if raced_commit is not None:
            raise RuntimeError(_existing_branch_message(branch_ref)) from exc
        raise
    return "created"


def _existing_branch_message(branch_ref: str) -> str:
    return (
        "Workspace branch export refuses to overwrite existing branch "
        f"'{branch_ref.removeprefix('refs/heads/')}'."
    )


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
    try:
        current_commit = branch_ref_commit(source, branch_ref)
    except _SymbolicBranchRefError as exc:
        return "failed_verification", str(exc)
    if current_commit is None:
        return "created", None
    if current_commit == result_commit:
        return "verified_existing", None
    return (
        "failed_verification",
        _existing_branch_message(branch_ref),
    )


def branch_commit(command: GitCommand, branch_ref: str) -> str | None:
    _reject_symbolic_branch_ref(command, branch_ref)
    try:
        command.run("show-ref", "--verify", "--quiet", branch_ref)
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 1:
            return None
        raise
    return command.text("rev-parse", "--verify", f"{branch_ref}^{{commit}}")


def _reject_symbolic_branch_ref(command: GitCommand, branch_ref: str) -> None:
    try:
        target = command.text("symbolic-ref", "-q", branch_ref)
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 1:
            return
        raise
    raise _SymbolicBranchRefError(
        "Workspace branch export destination is symbolic and was retained: "
        f"{branch_ref} -> {target}."
    )
