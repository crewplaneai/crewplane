import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from rich.console import Console

from crewplane.bootstrap import build_runtime_config_snapshot
from crewplane.core.preflight import (
    PreflightCompileOptions,
    PreflightExecutionPlan,
    compile_preflight_preview,
    load_workflow_source_for_preflight,
)
from crewplane.core.prompt_segments import PromptSegment, PromptSegmentRole
from crewplane.core.workflow.models import (
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.version import SCHEMA_VERSION
from tests.helpers.workspace_preflight import (
    compile_workflow_with_source_snapshot,
    init_git_repo,
    workspace_config,
    workspace_workflow,
)


@pytest.mark.parametrize(
    ("file_mode", "git_file_mode"), [(0o644, "100644"), (0o755, "100755")]
)
def test_workspace_enabled_file_tokens_compile_to_workspace_locators(
    tmp_path: Path,
    file_mode: int,
    git_file_mode: str,
) -> None:
    requirements = tmp_path / "docs" / "requirements.md"
    requirements.parent.mkdir()
    requirements.write_text("requirements\n", encoding="utf-8")
    requirements.chmod(file_mode)
    source_snapshot = init_git_repo(tmp_path)

    preview = compile_workflow_with_source_snapshot(
        tmp_path,
        workspace_workflow("Read {{file:docs/requirements.md}}"),
        source_snapshot,
    )

    assert preview.diagnostics == []
    assert preview.workflow_signature is not None
    assert len(preview.workspace_file_locators) == 2
    project_locator = preview.workspace_file_locators[0]
    reviewer_locator = preview.workspace_file_locators[1]
    assert project_locator.source_class == "project_initial_then_candidate"
    assert project_locator.target == "executor_prompt"
    assert project_locator.workspace_relative_path == "docs/requirements.md"
    assert project_locator.git_top_relative_path == "docs/requirements.md"
    assert project_locator.content_ref is not None
    assert project_locator.content_ref.startswith("workspace-files/")
    assert project_locator.git_blob is not None
    assert project_locator.git_file_mode == git_file_mode
    assert project_locator.byte_size == len(b"requirements\n")
    assert (
        project_locator.canonical_blob_sha256
        == hashlib.sha256(b"requirements\n").hexdigest()
    )
    assert project_locator.literal_path_verified is True
    assert project_locator.utf8_validated is True
    assert preview.workspace_file_payloads == {
        project_locator.content_ref: b"requirements\n"
    }
    assert reviewer_locator.source_class == "runtime_dynamic"
    assert reviewer_locator.content_ref is None
    assert reviewer_locator.git_blob is None
    assert preview.render_plans[0].streams[0].fragments[1].kind == (
        "workspace_file_locator"
    )
    assert preview.token_catalog[0].canonical_locator == project_locator.locator_id


@pytest.mark.parametrize(
    ("target", "source_class"),
    [
        ("executor_prompt", "project_initial"),
        ("reviewer_prompt", "project_initial_then_candidate"),
    ],
)
def test_persisted_worktree_locator_rejects_source_class_for_target(
    tmp_path: Path,
    target: str,
    source_class: str,
) -> None:
    requirements = tmp_path / "docs" / "requirements.md"
    requirements.parent.mkdir()
    requirements.write_text("requirements\n", encoding="utf-8")
    source_snapshot = init_git_repo(tmp_path)
    workflow = workspace_workflow("Read {{file:docs/requirements.md}}")
    workflow.nodes[0].providers.append(ProviderSpec(provider="alpha", role="reviewer"))
    preview = compile_workflow_with_source_snapshot(
        tmp_path,
        workflow,
        source_snapshot,
    )
    plan = PreflightExecutionPlan.from_preview(
        preview,
        run_id="run",
        run_key_name="workspace-preflight--run",
        project_root=tmp_path.as_posix(),
        context_root=".crewplane/execution-stages/workspace-preflight--run",
        manifest_root=(
            ".crewplane/execution-stages/workspace-preflight--run/manifests"
        ),
        created_at=datetime.now(UTC),
    )
    payload = plan.model_dump(mode="json")
    locator = next(
        record
        for record in payload["workspace_file_locators"]
        if record["target"] == target
    )
    locator["source_class"] = source_class
    if target == "reviewer_prompt":
        static_locator = next(
            record
            for record in payload["workspace_file_locators"]
            if record["target"] == "executor_prompt"
        )
        for field_name in (
            "content_ref",
            "git_blob",
            "git_file_mode",
            "byte_size",
            "canonical_blob_sha256",
            "injected_sha256",
            "literal_path_verified",
            "utf8_validated",
        ):
            locator[field_name] = static_locator[field_name]

    with pytest.raises(ValueError, match="source class conflicts"):
        PreflightExecutionPlan.model_validate_json(json.dumps(payload))


def test_workspace_imported_file_token_resolves_from_project_root(
    tmp_path: Path,
) -> None:
    root = tmp_path
    child_dir = root / ".crewplane" / "workflows" / "child"
    root_requirements = root / "docs" / "requirements.md"
    child_requirements = child_dir / "docs" / "requirements.md"
    root_requirements.parent.mkdir()
    child_requirements.parent.mkdir(parents=True)
    root_requirements.write_text("project requirements\n", encoding="utf-8")
    child_requirements.write_text("child requirements\n", encoding="utf-8")
    (child_dir / "workflow.task.md").write_text(
        "\n".join(
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Child",
                "worktrees:",
                "  primary:",
                "    kind: worktree",
                "nodes:",
                "  - id: implement",
                "    mode: sequential",
                "    providers: [alpha]",
                "---",
                "",
                "## implement",
                "",
                "Read {{file:docs/requirements.md}}",
            ]
        ),
        encoding="utf-8",
    )
    root_workflow = root / ".crewplane" / "workflows" / "root.task.md"
    root_workflow.write_text(
        "\n".join(
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Root",
                "imports:",
                "  - path: child/workflow.task.md",
                "    as: child",
                "nodes: []",
                "---",
            ]
        ),
        encoding="utf-8",
    )
    source_snapshot = init_git_repo(root)
    source = load_workflow_source_for_preflight(root_workflow, project_root=root)
    config = workspace_config()
    runtime_snapshot = build_runtime_config_snapshot(
        config=config,
        console=Console(file=None),
        no_live=True,
    )

    preview = compile_preflight_preview(
        source=source,
        config=config,
        runtime_snapshot=runtime_snapshot.snapshot,
        options=PreflightCompileOptions(
            project_root=root,
            state_dir=root / ".crewplane",
            fingerprint_key_policy="read_only",
            workspace_source_snapshot=source_snapshot,
        ),
    )

    assert preview.diagnostics == []
    assert {
        locator.workspace_relative_path for locator in preview.workspace_file_locators
    } == {"docs/requirements.md"}
    assert {
        locator.git_top_relative_path for locator in preview.workspace_file_locators
    } == {"docs/requirements.md"}
    locator = next(
        locator
        for locator in preview.workspace_file_locators
        if locator.source_class == "project_initial_then_candidate"
    )
    assert locator.source_root == root.as_posix()
    assert locator.source_root_relative_to_project == "."
    assert locator.workspace_relative_path == "docs/requirements.md"
    assert locator.git_top_relative_path == "docs/requirements.md"
    assert locator.content_ref is not None
    assert preview.workspace_file_payloads == {
        locator.content_ref: b"project requirements\n"
    }


def test_downstream_worktree_executor_file_locator_can_use_candidate_source(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("ready\n", encoding="utf-8")
    source_snapshot = init_git_repo(tmp_path)
    workflow = WorkflowPlan(
        name="workspace remediation file",
        worktrees={"primary": {"kind": "worktree"}},
        nodes=[
            WorkflowNode(
                id="implement",
                mode="sequential",
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="implement")
                ],
            ),
            WorkflowNode(
                id="fix",
                mode="sequential",
                needs=["implement"],
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[
                    PromptSegment(
                        role="shared",
                        content="Fix using {{file:docs/generated.md}}",
                    )
                ],
            ),
        ],
    )

    preview = compile_workflow_with_source_snapshot(
        tmp_path,
        workflow,
        source_snapshot,
    )

    assert preview.diagnostics == []
    executor_locators = [
        locator
        for locator in preview.workspace_file_locators
        if locator.target == "executor_prompt"
    ]
    assert len(executor_locators) == 1
    locator = executor_locators[0]
    assert locator.node_id == "fix"
    assert locator.source_class == "runtime_dynamic"
    assert locator.target == "executor_prompt"
    assert locator.content_ref is None


def test_worktree_executor_file_locator_after_project_root_node_uses_prior_candidate(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("ready\n", encoding="utf-8")
    source_snapshot = init_git_repo(tmp_path)
    workflow = WorkflowPlan(
        name="workspace transitive file",
        worktrees={"primary": {"kind": "worktree"}},
        nodes=[
            WorkflowNode(
                id="implement",
                mode="sequential",
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="implement")
                ],
            ),
            WorkflowNode(
                id="inspect",
                mode="sequential",
                needs=["implement"],
                worktree="none",
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="inspect")
                ],
            ),
            WorkflowNode(
                id="fix",
                mode="sequential",
                needs=["inspect"],
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[
                    PromptSegment(
                        role="shared",
                        content="Fix using {{file:README.md}}",
                    )
                ],
            ),
        ],
    )

    preview = compile_workflow_with_source_snapshot(
        tmp_path,
        workflow,
        source_snapshot,
    )

    assert preview.diagnostics == []
    assert preview.workflow_signature is not None
    executor_locators = [
        locator
        for locator in preview.workspace_file_locators
        if locator.target == "executor_prompt"
    ]
    assert len(executor_locators) == 1
    locator = executor_locators[0]
    assert locator.node_id == "fix"
    assert locator.source_class == "runtime_dynamic"
    assert locator.content_ref is None

    plan = PreflightExecutionPlan.from_preview(
        preview,
        run_id="run",
        run_key_name="workspace-transitive-file--run",
        project_root=tmp_path.as_posix(),
        context_root=".crewplane/execution-stages/workspace-transitive-file--run",
        manifest_root=(
            ".crewplane/execution-stages/workspace-transitive-file--run/manifests"
        ),
        created_at=datetime.now(UTC),
    )
    fix = next(node for node in plan.nodes if node.id == "fix")
    assert fix.workspace_policy is not None
    assert fix.workspace_policy.source_node_id == "implement"
