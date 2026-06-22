from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from orchestrator_cli.core.preflight.models import PreflightExecutionPlan

from .git import GitCommand, git
from .invocation import workspace_cleanup_on_success
from .locks import git_metadata_lock
from .worktree_refs import checked_ref, safe_ref_component

WorkspaceRunRefCleanup = Callable[[str], int]


def workspace_ref_cleanup_for_project(
    project_root: Path,
) -> WorkspaceRunRefCleanup | None:
    if shutil.which("git") is None:
        return None
    try:
        repo_root = Path(
            git(project_root).text("rev-parse", "--show-toplevel")
        ).resolve(strict=False)
        common_git_dir = resolve_common_git_dir(repo_root)
    except subprocess.CalledProcessError:
        return None

    def cleanup_run_refs(run_key_name: str) -> int:
        return delete_run_workspace_refs(repo_root, common_git_dir, run_key_name)

    return cleanup_run_refs


def delete_run_workspace_refs(
    repo_root: Path,
    common_git_dir: Path,
    run_key_name: str,
) -> int:
    scope = run_workspace_ref_scope(repo_root, run_key_name)
    command = git(repo_root)
    with git_metadata_lock(common_git_dir):
        refs = run_workspace_refs(command, scope)
        for ref_name in refs:
            command.run("update-ref", "-d", ref_name)
    return len(refs)


def cleanup_plan_workspace_refs(plan: PreflightExecutionPlan) -> int:
    source = plan.workspace_source
    if source is None or not workspace_cleanup_on_success(plan):
        return 0
    return delete_run_workspace_refs(
        Path(source.git_top_level),
        Path(source.common_git_dir),
        plan.run_key_name,
    )


def run_workspace_ref_scope(repo_root: Path, run_key_name: str) -> str:
    return checked_ref(
        repo_root,
        f"refs/orchestrator-cli/runs/{safe_ref_component(run_key_name)}",
    )


def run_workspace_refs(command: GitCommand, scope: str) -> tuple[str, ...]:
    output = command.text("for-each-ref", "--format=%(refname)", scope)
    return tuple(line for line in output.splitlines() if line)


def resolve_common_git_dir(repo_root: Path) -> Path:
    raw_path = git(repo_root).text("rev-parse", "--git-common-dir")
    common_git_dir = Path(raw_path)
    if not common_git_dir.is_absolute():
        common_git_dir = repo_root / common_git_dir
    return common_git_dir.resolve(strict=False)
