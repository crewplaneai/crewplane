from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from crewplane.runtime.workspace import PreparedWorkspace, prepare_invocation_workspace
from crewplane.runtime.workspace.worktree import (
    remove_worktree_workspace,
)
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.workspace_service import (
    create_git_repo,
    read_json_object,
    run_git_text,
    workspace_invocation_context,
    workspace_invocation_request,
    workspace_output_manager,
    workspace_plan,
)


def prepared_lineage_workspace(tmp_path: Path) -> tuple[Path, PreparedWorkspace]:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=False,
        kind="worktree",
    )
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    (prepared.cwd / "result.txt").write_text("captured\n", encoding="utf-8")
    assert prepared.worktree_capture is not None
    return repo, prepared


def published_lineage_workspace(
    tmp_path: Path,
) -> tuple[Path, PreparedWorkspace, dict[str, object]]:
    repo, prepared = prepared_lineage_workspace(tmp_path)
    prepared.mark_succeeded()
    assert prepared.state_path is not None
    return repo, prepared, read_json_object(prepared.state_path)


def remove_published_workspace(
    repo: Path,
    prepared: PreparedWorkspace,
    state: dict[str, object],
) -> None:
    refs = state["refs"]
    assert isinstance(refs, dict)
    run_git_text(repo, "update-ref", "-d", str(refs["candidate"]))
    run_git_text(repo, "update-ref", "-d", str(refs["result"]))
    assert prepared.workspace_path is not None
    assert prepared.worktree_capture is not None
    remove_worktree_workspace(
        prepared.worktree_capture.source,
        prepared.workspace_path,
        prepared.worktree_capture.git_dir,
    )


def ref_oid(repo: Path, ref_name: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", repo.as_posix(), "rev-parse", "--verify", ref_name],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None
