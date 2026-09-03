from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from crewplane.runtime.workspace.git import git
from crewplane.runtime.workspace.worktree.result_validation import (
    validate_portable_path_collisions,
    validate_result_tree,
)
from tests.helpers.workspace_service import create_git_repo, run_git_text


def test_validate_result_tree_rejects_reserved_paths_under_project_root(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    reserved_path = repo / "packages" / "app" / ".crewplane" / "execution-results"
    reserved_path.mkdir(parents=True)
    (reserved_path / "leak.txt").write_text("runtime artifact\n", encoding="utf-8")
    run_git_text(repo, "add", "packages/app/.crewplane/execution-results/leak.txt")
    tree = run_git_text(repo, "write-tree")

    with pytest.raises(RuntimeError, match="reserved runtime artifact paths"):
        validate_result_tree(repo, tree, "packages/app")


def test_validate_result_tree_rejects_missing_blob_object(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    missing_oid = "f" * 40
    tree = (
        git(repo)
        .run_with_input(
            f"100644 blob {missing_oid}\tmissing.txt\n".encode(),
            "mktree",
            "--missing",
        )
        .stdout.decode()
        .strip()
    )

    with pytest.raises(RuntimeError, match="missing or non-blob object"):
        validate_result_tree(repo, tree)


@pytest.mark.parametrize(
    ("left_path", "right_path"),
    (
        ("Case.txt", "case.txt"),
        ("Cafe\u0301.txt", "Café.txt"),
    ),
)
def test_validate_portable_path_collisions(
    left_path: str,
    right_path: str,
) -> None:
    with pytest.raises(RuntimeError, match="case or Unicode normalization"):
        validate_portable_path_collisions((left_path, right_path))
