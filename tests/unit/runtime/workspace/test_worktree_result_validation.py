from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from orchestrator_cli.runtime.workspace.worktree.result_validation import (
    validate_result_tree,
)
from tests.helpers.workspace_service import create_git_repo, run_git_text


def test_validate_result_tree_rejects_reserved_paths_under_project_root(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    reserved_path = repo / "packages" / "app" / ".orchestrator" / "execution-results"
    reserved_path.mkdir(parents=True)
    (reserved_path / "leak.txt").write_text("runtime artifact\n", encoding="utf-8")
    run_git_text(repo, "add", "packages/app/.orchestrator/execution-results/leak.txt")
    tree = run_git_text(repo, "write-tree")

    with pytest.raises(RuntimeError, match="reserved runtime artifact paths"):
        validate_result_tree(repo, tree, "packages/app")
