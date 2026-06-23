from pathlib import Path

import pytest
from pydantic import ValidationError

from crewplane.core.preflight.models import WorkspaceSelectionRecord
from tests.helpers.workspace_preflight import (
    compile_with_source_snapshot,
    compile_workflow_with_source_snapshot,
    compile_workspace_preview,
    workspace_source_snapshot,
    workspace_workflow,
)
from tests.helpers.workspace_records import WORKTREE_CONTRACT_PAYLOAD


def test_workspace_enabled_core_preflight_requires_source_snapshot(
    tmp_path: Path,
) -> None:
    requirements = tmp_path / "docs" / "requirements.md"
    requirements.parent.mkdir()
    requirements.write_text("requirements\n", encoding="utf-8")

    preview = compile_workflow_with_source_snapshot(
        tmp_path,
        workspace_workflow("Read {{file:docs/requirements.md}}"),
        None,
    )

    assert preview.workflow_signature is None
    assert preview.static_resources == []
    assert preview.workspace_file_locators == []
    assert [(item.code, item.phase) for item in preview.diagnostics] == [
        ("WORKSPACE-GIT-CONTRACT", "worktree_contract")
    ]
    assert "trusted workspace source snapshot" in preview.diagnostics[0].message


def test_workspace_enabled_preflight_records_policy_and_source(
    tmp_path: Path,
) -> None:
    preview = compile_workspace_preview(tmp_path, "a" * 40)

    assert preview.diagnostics == []
    assert preview.workspace_source is not None
    assert (
        preview.workspace_source.worktree_contract.model_dump(mode="json")
        == WORKTREE_CONTRACT_PAYLOAD
    )
    assert preview.nodes[0].workspace_policy is not None
    assert preview.nodes[0].workspace_policy.enabled is True
    assert preview.nodes[0].workspace_policy.declaration_kind == "worktree"
    assert preview.nodes[0].workspace_policy.source_kind == "project"
    assert preview.nodes[0].workspace_policy.materialization == "worktree_checkout"
    assert preview.nodes[0].workspace_policy.logical_worktree_name == "primary"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("declaration_kind", "workspace"),
        ("source_kind", "candidate"),
        ("clean_start", "dirty_ok"),
        ("materialization", "detached_checkout"),
    ],
)
def test_workspace_selection_record_rejects_invalid_literal_values(
    field: str,
    value: str,
) -> None:
    payload = {
        "enabled": True,
        "logical_worktree_name": "primary",
        "declaration_kind": "worktree",
        "source_kind": "project",
        "clean_start": "strict",
        "materialization": "worktree_checkout",
        field: value,
    }

    with pytest.raises(ValidationError):
        WorkspaceSelectionRecord.model_validate(payload)


def test_workspace_source_identity_changes_workflow_signature(
    tmp_path: Path,
) -> None:
    first = compile_workspace_preview(tmp_path, "a" * 40)
    second = compile_workspace_preview(tmp_path, "d" * 40)

    assert first.workflow_signature is not None
    assert second.workflow_signature is not None
    assert first.workflow_signature != second.workflow_signature


def test_workspace_source_signature_excludes_local_git_probe_details(
    tmp_path: Path,
) -> None:
    first = compile_with_source_snapshot(
        tmp_path,
        workspace_source_snapshot(
            "a" * 40,
            git_version="git version 2.34.1",
            git_top_level="/repo-one",
            active_git_dir="/repo-one/.git",
            common_git_dir="/repo-one/.git",
        ),
    )
    second = compile_with_source_snapshot(
        tmp_path,
        workspace_source_snapshot(
            "a" * 40,
            git_version="git version 2.47.1",
            git_top_level="/repo-two",
            active_git_dir="/repo-two/.git/worktrees/current",
            common_git_dir="/repo-two/.git",
        ),
    )

    assert first.workflow_signature is not None
    assert second.workflow_signature is not None
    assert first.workflow_signature == second.workflow_signature
