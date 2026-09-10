from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.runtime.workspace.git import GitCommand
from crewplane.runtime.workspace.worktree.inspection import (
    changed_paths,
    filesystem_gitattributes_drift_detected,
    filesystem_gitignore_drift_detected,
    inspect_disposable_checkout,
    reject_byte_transforming_attributes,
)
from crewplane.runtime.workspace.worktree.protected_refs import ProtectedRefSnapshot
from tests.helpers import isolated_git as isolated_git_support
from tests.helpers.isolated_git import IsolatedGit
from tests.helpers.workspace_service import create_git_repo, run_git_text

isolated_git = isolated_git_support.isolated_git


@pytest.mark.parametrize("operation", ["head", "status", "attributes"])
def test_disposable_checkout_records_inspection_failures(
    tmp_path: Path, isolated_git: IsolatedGit, operation: str
) -> None:
    del isolated_git
    repo = create_git_repo(tmp_path)
    head = run_git_text(repo, "rev-parse", "HEAD")
    checkout = tmp_path / "checkout"
    run_git_text(repo, "worktree", "add", "--detach", str(checkout), head)
    real_run = GitCommand.run

    def run_with_inspection_failure(
        command: GitCommand, *args: str
    ) -> subprocess.CompletedProcess[bytes]:
        matches = (
            (operation == "head" and args == ("rev-parse", "HEAD^{commit}"))
            or (operation == "status" and args[0] == "status")
            or (operation == "attributes" and args[0] == "ls-tree")
        )
        if matches:
            raise subprocess.CalledProcessError(
                128, ["git", *args], stderr=b"inspection unavailable"
            )
        return real_run(command, *args)

    with patch.object(GitCommand, "run", new=run_with_inspection_failure):
        result = inspect_disposable_checkout(
            checkout, head, ProtectedRefSnapshot((), ()), repo, repo / ".git"
        )
    assert result.final_head == (None if operation == "head" else head)
    assert result.changed_path_count == 0
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0]["level"] == "warning"
    assert "inspection unavailable" in result.diagnostics[0]["message"]


def test_disposable_checkout_reports_head_attributes_and_reserved_file_changes(
    tmp_path: Path, isolated_git: IsolatedGit
) -> None:
    del isolated_git
    repo = create_git_repo(tmp_path)
    head = run_git_text(repo, "rev-parse", "HEAD")
    checkout = tmp_path / "checkout"
    run_git_text(repo, "worktree", "add", "--detach", str(checkout), head)
    run_git_text(checkout, "commit", "--allow-empty", "-m", "provider commit")
    (checkout / ".gitattributes").write_text("*.txt text\n", encoding="utf-8")
    reserved = checkout / ".crewplane" / "execution-results"
    reserved.mkdir(parents=True)
    (reserved / "forged.md").write_text("untrusted", encoding="utf-8")
    result = inspect_disposable_checkout(
        checkout, head, ProtectedRefSnapshot((), ()), repo, repo / ".git"
    )
    assert result.final_head != head
    assert result.changed_path_count == 2
    messages = [item["message"] for item in result.diagnostics]
    assert len(messages) == 4
    assert any("moved HEAD" in message for message in messages)
    assert any("changed .gitattributes" in message for message in messages)
    assert any("reserved runtime paths" in message for message in messages)


@pytest.mark.parametrize("filename", [".gitattributes", ".gitignore"])
def test_policy_file_symlink_replacement_is_drift(
    tmp_path: Path, isolated_git: IsolatedGit, filename: str
) -> None:
    del isolated_git
    repo = create_git_repo(tmp_path)
    path = repo / filename
    path.write_text("original\n", encoding="utf-8")
    run_git_text(repo, "add", filename)
    run_git_text(repo, "commit", "-m", "policy")
    head = run_git_text(repo, "rev-parse", "HEAD")
    path.unlink()
    path.symlink_to("README.md")
    inspect_policy = (
        filesystem_gitignore_drift_detected
        if filename == ".gitignore"
        else filesystem_gitattributes_drift_detected
    )
    assert inspect_policy(repo, head)


def test_git_status_parser_skips_truncated_records_and_keeps_paired_rename(
    tmp_path: Path,
) -> None:
    result = subprocess.CompletedProcess(["git"], 0, b"?\0R  new.txt\0old.txt\0", b"")
    with patch.object(GitCommand, "run", return_value=result):
        assert changed_paths(tmp_path) == ("new.txt", "old.txt")


def test_attribute_capture_rejects_truncated_record_stream(tmp_path: Path) -> None:
    result = subprocess.CompletedProcess(["git"], 0, b"file.txt\0filter\0", b"")
    with (
        patch.object(GitCommand, "run", return_value=result),
        pytest.raises(RuntimeError, match="invalid attribute record stream"),
    ):
        reject_byte_transforming_attributes(tmp_path, ("file.txt",))


def test_policy_inspection_rejects_malformed_tree_record(tmp_path: Path) -> None:
    result = subprocess.CompletedProcess(["git"], 0, b"blob\t.gitattributes\0", b"")
    with (
        patch.object(GitCommand, "run", return_value=result),
        pytest.raises(RuntimeError, match="invalid .gitattributes tree entry"),
    ):
        filesystem_gitattributes_drift_detected(tmp_path, "a" * 40)
