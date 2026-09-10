from pathlib import Path

from crewplane.core.prompt_segments import PromptSegment, PromptSegmentRole
from crewplane.core.workflow.models import (
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from tests.helpers.terminal_results import RESULT_SOURCE_TOKEN, write_result_source
from tests.helpers.workspace_preflight import (
    compile_workflow_with_source_snapshot,
    init_git_repo,
    workspace_workflow,
)


def test_workspace_enabled_input_node_uses_static_file_content(
    tmp_path: Path,
) -> None:
    requirements = tmp_path / "docs" / "requirements.md"
    requirements.parent.mkdir()
    requirements.write_text("requirements\n", encoding="utf-8")
    source_snapshot = init_git_repo(tmp_path)
    workflow = WorkflowPlan(
        name="workspace input",
        nodes=[
            WorkflowNode(
                id="requirements",
                mode="input",
                source="{{file:docs/requirements.md}}",
            )
        ],
    )

    preview = compile_workflow_with_source_snapshot(
        tmp_path,
        workflow,
        source_snapshot,
    )

    assert preview.diagnostics == []
    assert preview.workspace_file_locators == []
    assert preview.workspace_file_payloads == {}
    assert preview.nodes[0].input_content_ref is not None
    assert preview.nodes[0].input_workspace_file_locator_id is None
    assert preview.nodes[0].workspace_policy is None


def test_workspace_enabled_input_node_stays_static_when_provider_opts_out(
    tmp_path: Path,
) -> None:
    requirements = tmp_path / "docs" / "requirements.md"
    requirements.parent.mkdir()
    requirements.write_text("requirements\n", encoding="utf-8")
    source_snapshot = init_git_repo(tmp_path)
    workflow = WorkflowPlan(
        name="workspace input",
        worktrees={"primary": {"kind": "worktree"}},
        nodes=[
            WorkflowNode(
                id="requirements",
                mode="input",
                source="{{file:docs/requirements.md}}",
            ),
            WorkflowNode(
                id="implement",
                mode="sequential",
                needs=["requirements"],
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="run")
                ],
                worktree="none",
            ),
        ],
    )

    preview = compile_workflow_with_source_snapshot(
        tmp_path,
        workflow,
        source_snapshot,
    )

    assert preview.diagnostics == []
    assert preview.workspace_file_locators == []
    assert preview.nodes[0].input_content_ref is not None
    assert preview.nodes[0].input_workspace_file_locator_id is None
    assert preview.nodes[1].workspace_policy is None


def test_workspace_enabled_input_node_uses_workspace_file_locator(
    tmp_path: Path,
) -> None:
    requirements = tmp_path / "docs" / "requirements.md"
    requirements.parent.mkdir()
    requirements.write_text("requirements\n", encoding="utf-8")
    source_snapshot = init_git_repo(tmp_path)
    workflow = WorkflowPlan(
        name="workspace input",
        worktrees={"primary": {"kind": "worktree"}},
        nodes=[
            WorkflowNode(
                id="requirements",
                mode="input",
                source="{{file:docs/requirements.md}}",
            ),
            WorkflowNode(
                id="implement",
                mode="sequential",
                needs=["requirements"],
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="run")
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
    assert preview.static_resources == []
    assert len(preview.workspace_file_locators) == 1
    locator = preview.workspace_file_locators[0]
    assert locator.node_id == "requirements"
    assert locator.target == "input_output"
    assert locator.source_class == "project_initial"
    assert locator.workspace_relative_path == "docs/requirements.md"
    assert locator.git_top_relative_path == "docs/requirements.md"
    assert locator.content_ref is not None
    assert preview.workspace_file_payloads == {locator.content_ref: b"requirements\n"}
    assert preview.nodes[0].input_content_ref is None
    assert preview.nodes[0].input_workspace_file_locator_id == locator.locator_id
    assert preview.nodes[0].workspace_policy is None
    assert preview.nodes[1].workspace_policy is not None
    assert preview.token_catalog[0].canonical_locator == locator.locator_id
    assert preview.token_catalog[0].resolved["kind"] == "workspace_file_locator"


def test_workspace_enabled_input_node_keeps_terminal_result_static(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("project\n", encoding="utf-8")
    source_snapshot = init_git_repo(tmp_path)
    write_result_source(tmp_path, status="failed")
    workflow = WorkflowPlan(
        name="workspace terminal input",
        worktrees={"primary": {"kind": "worktree"}},
        nodes=[
            WorkflowNode(
                id="prior-result",
                mode="input",
                source=RESULT_SOURCE_TOKEN,
            ),
            WorkflowNode(
                id="implement",
                mode="sequential",
                needs=["prior-result"],
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="run")
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
    assert preview.workspace_file_locators == []
    assert len(preview.static_resources) == 1
    assert preview.nodes[0].input_content_ref is not None
    assert preview.nodes[0].input_workspace_file_locator_id is None
    assert preview.nodes[1].workspace_policy is not None


def test_workspace_enabled_prompt_keeps_terminal_result_static(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("project\n", encoding="utf-8")
    source_snapshot = init_git_repo(tmp_path)
    write_result_source(tmp_path, status="failed")
    workflow = workspace_workflow(prompt=f"Review {RESULT_SOURCE_TOKEN}")

    preview = compile_workflow_with_source_snapshot(
        tmp_path,
        workflow,
        source_snapshot,
    )

    assert preview.diagnostics == []
    assert preview.workspace_file_locators == []
    assert len(preview.static_resources) == 1
    assert set(preview.static_file_payloads.values()) == {b"prior result"}
    assert preview.render_plans[0].streams[0].fragments[1].kind == (
        "static_file_content"
    )


def test_worktree_node_after_input_uses_initial_then_candidate_file_locator(
    tmp_path: Path,
) -> None:
    requirements = tmp_path / "docs" / "requirements.md"
    generated = tmp_path / "docs" / "generated.md"
    requirements.parent.mkdir()
    requirements.write_text("requirements\n", encoding="utf-8")
    generated.write_text("generated\n", encoding="utf-8")
    source_snapshot = init_git_repo(tmp_path)
    workflow = WorkflowPlan(
        name="workspace input then implement",
        worktrees={"primary": {"kind": "worktree"}},
        nodes=[
            WorkflowNode(
                id="requirements",
                mode="input",
                source="{{file:docs/requirements.md}}",
            ),
            WorkflowNode(
                id="implement",
                mode="sequential",
                needs=["requirements"],
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[
                    PromptSegment(
                        role="shared",
                        content="Read {{file:docs/generated.md}}",
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
    implement_locator = next(
        locator
        for locator in preview.workspace_file_locators
        if locator.node_id == "implement"
    )
    assert implement_locator.source_class == "project_initial_then_candidate"
    assert implement_locator.content_ref is not None
    assert preview.workspace_file_payloads[implement_locator.content_ref] == (
        b"generated\n"
    )


def test_workspace_enabled_allowlisted_absolute_input_source_remains_static(
    tmp_path: Path,
) -> None:
    external_dir = tmp_path.parent / f"{tmp_path.name}-external"
    external_dir.mkdir()
    external_file = external_dir / "requirements.md"
    external_file.write_text("external requirements\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("readme\n", encoding="utf-8")
    source_snapshot = init_git_repo(tmp_path)
    workflow = WorkflowPlan(
        name="workspace input",
        worktrees={"primary": {"kind": "worktree"}},
        nodes=[
            WorkflowNode(
                id="requirements",
                mode="input",
                source=f"{{{{file:{external_file.as_posix()}}}}}",
            ),
            WorkflowNode(
                id="implement",
                mode="sequential",
                needs=["requirements"],
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="run")
                ],
            ),
        ],
    )

    preview = compile_workflow_with_source_snapshot(
        tmp_path,
        workflow,
        source_snapshot,
        allowed_template_paths=(external_dir,),
    )

    assert preview.diagnostics == []
    assert preview.workspace_file_locators == []
    assert [resource.raw_path for resource in preview.static_resources] == [
        external_file.as_posix()
    ]
    assert preview.nodes[0].input_content_ref is not None
    assert preview.nodes[0].input_workspace_file_locator_id is None
