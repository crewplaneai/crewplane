from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from crewplane.core.preflight import PreflightExecutionPlan
from crewplane.core.workflow.models import WorkflowPlan
from tests.helpers import isolated_git as isolated_git_support
from tests.helpers.isolated_git import IsolatedGit
from tests.helpers.resume import replace_plan_fields
from tests.helpers.workspace_preflight import (
    compile_workflow_with_source_snapshot,
    init_git_repo,
)

isolated_git = isolated_git_support.isolated_git


@pytest.fixture
def workspace_plan_payload(tmp_path: Path, isolated_git: IsolatedGit) -> dict[str, Any]:
    (tmp_path / "context.md").write_text("Review context", encoding="utf-8")
    snapshot = init_git_repo(tmp_path)
    assert isolated_git.run_text(tmp_path, "status", "--porcelain") == ""
    workflow = WorkflowPlan.model_validate(
        {
            "name": "Workspace contract",
            "worktrees": {"primary": {"kind": "snapshot"}},
            "nodes": [
                {
                    "id": "build",
                    "mode": "parallel",
                    "providers": [{"provider": "alpha"}],
                    "prompt_segments": [
                        {"role": "shared", "content": "{{file:context.md}}"}
                    ],
                },
                {
                    "id": "review",
                    "mode": "parallel",
                    "providers": [{"provider": "alpha"}],
                    "prompt_segments": [
                        {"role": "shared", "content": "{{file:context.md}}"}
                    ],
                },
                {"id": "input", "mode": "input", "source": "{{file:context.md}}"},
            ],
        }
    )
    preview = compile_workflow_with_source_snapshot(tmp_path, workflow, snapshot)
    assert preview.diagnostics == []
    plan = PreflightExecutionPlan.from_preview(
        preview,
        run_id="run",
        run_key_name="workspace-contract--run",
        project_root=str(tmp_path),
        context_root=".crewplane/execution-stages/workspace-contract--run",
        manifest_root=".crewplane/execution-stages/workspace-contract--run/manifests",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    return plan.model_dump(mode="json")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"workspace_file_locators/0/node_id": "missing"}, "reference unknown nodes"),
        (
            {"workspace_file_locators/1/node_id": "review"},
            "node identity is inconsistent",
        ),
        (
            {"workspace_file_locators/1/target": "reviewer_prompt"},
            "target conflicts with its stream",
        ),
        (
            {"render_plans/0/streams/0/fragments/0/locator/locator_id": "missing"},
            "references unknown workspace locator",
        ),
        ({"nodes/0/workspace_policy": None}, "without an enabled workspace policy"),
        (
            {"nodes/2/input_workspace_file_locator_id": "missing"},
            "references an invalid workspace locator",
        ),
        (
            {"workspace_file_locators/0/target": "executor_prompt"},
            "has the wrong target",
        ),
        (
            {"workspace_file_locators/0/node_id": "review"},
            "references an invalid workspace locator",
        ),
        ({"workspace_file_locators/0/raw_path": " "}, "text fields cannot be blank"),
        (
            {"workspace_file_locators/0/literal_path_verified": False},
            "require verified literal UTF-8 bytes",
        ),
        (
            {"workspace_file_locators/0/utf8_validated": False},
            "require verified literal UTF-8 bytes",
        ),
    ],
)
def test_persisted_workspace_plan_rejects_broken_locator_ownership(
    workspace_plan_payload: dict[str, Any], changes: dict[str, object], message: str
) -> None:
    replace_plan_fields(workspace_plan_payload, changes)

    with pytest.raises(ValidationError, match=message):
        PreflightExecutionPlan.model_validate(workspace_plan_payload)


def test_persisted_workspace_plan_rejects_a_locator_without_a_consumer(
    workspace_plan_payload: dict[str, Any],
) -> None:
    locator = deepcopy(workspace_plan_payload["workspace_file_locators"][0])
    locator["locator_id"] = "unowned"
    locator["occurrence_id"] = "unowned"
    workspace_plan_payload["workspace_file_locators"].append(locator)

    with pytest.raises(
        ValidationError, match="must each have exactly one owner: unowned"
    ):
        PreflightExecutionPlan.model_validate(workspace_plan_payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "fingerprint_metadata",
            {},
            "fingerprint metadata must include 'payload_version'",
        ),
        (
            "value_fingerprints",
            [{}],
            "value fingerprint at index 0 must include 'fingerprint_payload_version'",
        ),
    ],
)
def test_persisted_workspace_plan_requires_explicit_fingerprint_payload_versions(
    workspace_plan_payload: dict[str, Any], field: str, value: object, message: str
) -> None:
    workspace_plan_payload[field] = value

    with pytest.raises(ValidationError, match=message):
        PreflightExecutionPlan.model_validate(workspace_plan_payload)
