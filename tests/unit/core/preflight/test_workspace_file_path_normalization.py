from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from crewplane.core.preflight import PreflightExecutionPlan
from tests.helpers import isolated_git as isolated_git_support
from tests.helpers.isolated_git import IsolatedGit
from tests.helpers.workspace_preflight import (
    compile_workflow_with_source_snapshot,
    init_git_repo,
    workspace_workflow,
)

isolated_git = isolated_git_support.isolated_git


def test_workspace_file_token_normalizes_internal_parent_components(
    tmp_path: Path, isolated_git: IsolatedGit
) -> None:
    payload = b"Expected project requirements.\n"
    requirements = tmp_path / "requirements.md"
    requirements.write_bytes(payload)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "requirements.md").write_text(
        "Unrelated nested requirements.\n", encoding="utf-8"
    )
    source_snapshot = init_git_repo(tmp_path)
    assert isolated_git.run_text(tmp_path, "status", "--porcelain") == ""

    preview = compile_workflow_with_source_snapshot(
        tmp_path,
        workspace_workflow("Read {{file:docs/../requirements.md}}"),
        source_snapshot,
    )

    assert preview.diagnostics == []
    assert preview.workspace_file_locators
    assert {
        locator.workspace_relative_path for locator in preview.workspace_file_locators
    } == {"requirements.md"}
    assert {
        locator.git_top_relative_path for locator in preview.workspace_file_locators
    } == {"requirements.md"}
    project_locator = next(
        locator
        for locator in preview.workspace_file_locators
        if locator.content_ref is not None
    )
    assert project_locator.content_ref is not None
    assert project_locator.canonical_blob_sha256 == hashlib.sha256(payload).hexdigest()
    assert preview.workspace_file_payloads == {project_locator.content_ref: payload}
    plan = PreflightExecutionPlan.from_preview(
        preview=preview,
        run_id="normalized-file-run",
        run_key_name="normalized-file-run",
        project_root=tmp_path.as_posix(),
        context_root=tmp_path.as_posix(),
        manifest_root=(tmp_path / ".crewplane" / "manifests").as_posix(),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    restored = PreflightExecutionPlan.model_validate_json(plan.model_dump_json())

    assert restored.workspace_file_locators == plan.workspace_file_locators
    assert not (tmp_path / ".crewplane").exists()
