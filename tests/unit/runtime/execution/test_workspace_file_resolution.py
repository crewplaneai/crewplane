from __future__ import annotations

from pathlib import Path

import pytest

import crewplane.runtime.execution.workspace_files.resolution as workspace_file_resolution
from crewplane.core.preflight.models import PreflightExecutionPlan, WorkspaceFileLocator
from crewplane.core.workspace.git_reads import GitTreeRecord
from crewplane.runtime.execution.errors import NodeExecutionError
from crewplane.runtime.execution.workspace_files import read_dynamic_locator_blob
from crewplane.runtime.workspace.worktree import WorktreeSourceRef
from crewplane.runtime.workspace.worktree.temporary_refs import TemporaryRefOwner
from tests.helpers.workspace_service import create_git_repo, workspace_plan


@pytest.mark.parametrize("mode", ["120000", "160000", "040000", "100664"])
def test_read_dynamic_locator_blob_rejects_unsupported_file_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    plan, locator, source, owner = _dynamic_blob_inputs(tmp_path)

    def unsupported_record(
        git_top_level: str,
        source_commit: str,
        git_top_relative_path: str,
    ) -> GitTreeRecord:
        assert git_top_level
        assert source_commit
        return GitTreeRecord(
            mode=mode,
            object_type="blob",
            object_id="a" * 40,
            path=git_top_relative_path,
        )

    def unexpected_blob_read(git_top_level: str, object_id: str) -> bytes:
        raise AssertionError(f"unexpected blob read from {git_top_level}: {object_id}")

    monkeypatch.setattr(workspace_file_resolution, "git_ls_tree", unsupported_record)
    monkeypatch.setattr(
        workspace_file_resolution,
        "git_cat_blob",
        unexpected_blob_read,
    )

    with pytest.raises(NodeExecutionError, match="regular Git blob"):
        read_dynamic_locator_blob(plan, locator, source, owner)


@pytest.mark.parametrize("payload", [b"\xff", b"text\x00"])
def test_read_dynamic_locator_blob_rejects_non_text_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
) -> None:
    plan, locator, source, owner = _dynamic_blob_inputs(tmp_path)

    def regular_blob_record(
        git_top_level: str,
        source_commit: str,
        git_top_relative_path: str,
    ) -> GitTreeRecord:
        assert git_top_level
        assert source_commit
        return GitTreeRecord(
            mode="100644",
            object_type="blob",
            object_id="a" * 40,
            path=git_top_relative_path,
        )

    def invalid_blob_content(git_top_level: str, object_id: str) -> bytes:
        assert git_top_level
        assert object_id == "a" * 40
        return payload

    monkeypatch.setattr(
        workspace_file_resolution,
        "git_ls_tree",
        regular_blob_record,
    )
    monkeypatch.setattr(
        workspace_file_resolution,
        "git_cat_blob",
        invalid_blob_content,
    )

    with pytest.raises(NodeExecutionError, match="UTF-8 text without NUL bytes"):
        read_dynamic_locator_blob(plan, locator, source, owner)


def _dynamic_blob_inputs(
    tmp_path: Path,
) -> tuple[
    PreflightExecutionPlan,
    WorkspaceFileLocator,
    WorktreeSourceRef,
    TemporaryRefOwner,
]:
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(repo, tmp_path / "cache", cleanup_on_success=True)
    workspace_source = plan.workspace_source
    assert workspace_source is not None
    locator = WorkspaceFileLocator(
        locator_id="workspace-file-dynamic",
        occurrence_id="implement:executor:0:file:README.md",
        node_id="implement",
        target="executor_prompt",
        source_class="runtime_dynamic",
        raw_token="{{file:README.md}}",
        raw_path="README.md",
        source_root=repo.as_posix(),
        source_root_relative_to_project=".",
        project_root_relative_to_git_top=".",
        git_top_relative_path="README.md",
        workspace_relative_path="README.md",
    )
    source = WorktreeSourceRef(
        source_kind="project",
        source_node_id=None,
        source_commit=workspace_source.run_base_commit,
        source_tree=workspace_source.source_tree,
    )
    return plan, locator, source, TemporaryRefOwner(tmp_path / "temporary-refs.json")
