from __future__ import annotations

import pytest
from pydantic import ValidationError

from crewplane.core.preflight import render_plans
from crewplane.core.preflight.models import (
    RenderStream,
    TokenCatalogEntry,
    WorkspaceFileLocator,
    WorkspaceFileSourceClass,
    WorkspaceFileTarget,
)
from crewplane.core.preflight.references import TemplateReference
from crewplane.core.preflight.workspace.files.locators import locator_source_class
from crewplane.core.prompt_segments import PromptSegment, PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import ProviderSpec, WorkflowNode, WorkflowPlan


def test_provider_role_validates_and_serializes_lowercase_values() -> None:
    provider = ProviderSpec(provider="alpha", role=ProviderRole.REVIEWER)

    assert provider.role == ProviderRole.REVIEWER
    assert provider.model_dump(mode="json")["role"] == "reviewer"


@pytest.mark.parametrize("role", ["Reviewer", " reviewer ", "author"])
def test_provider_role_rejects_invalid_or_mixed_case_values(role: str) -> None:
    with pytest.raises(ValidationError):
        ProviderSpec(provider="alpha", role=role)


def test_prompt_segment_role_preserves_shared_and_canonical_roles() -> None:
    shared = PromptSegment(role=PromptSegmentRole.SHARED, content="shared")
    executor = PromptSegment(role=PromptSegmentRole.EXECUTOR, content="executor")

    assert shared.role == PromptSegmentRole.SHARED
    assert executor.role == PromptSegmentRole.EXECUTOR
    assert shared.model_dump(mode="json") == {"role": "shared", "content": "shared"}


@pytest.mark.parametrize("role", ["Shared", "executor ", "author"])
def test_prompt_segment_role_rejects_invalid_values(role: str) -> None:
    with pytest.raises(ValidationError):
        PromptSegment(role=role, content="body")


def test_render_stream_target_role_serializes_lowercase_values() -> None:
    stream = RenderStream(target_role=ProviderRole.EXECUTOR, fragments=[])

    assert stream.target_role == ProviderRole.EXECUTOR
    assert stream.model_dump(mode="json") == {
        "target_role": "executor",
        "fragments": [],
    }


def test_token_catalog_target_role_serializes_lowercase_values() -> None:
    entry = TokenCatalogEntry(
        occurrence_id="build:executor:0:0",
        node_id="build",
        target_role=ProviderRole.EXECUTOR,
        source_role=PromptSegmentRole.SHARED,
        raw_token="{{file:README.md}}",
        token_kind="file",
        fragment_index=0,
        signature="sig",
    )

    dumped = entry.model_dump(mode="json")

    assert dumped["target_role"] == "executor"
    assert dumped["source_role"] == "shared"


def test_workspace_prompt_target_mapping_uses_workspace_target_enum() -> None:
    assert (
        render_plans.workspace_file_target_for_role(ProviderRole.EXECUTOR)
        == WorkspaceFileTarget.EXECUTOR_PROMPT
    )
    assert (
        render_plans.workspace_file_target_for_role(ProviderRole.REVIEWER)
        == WorkspaceFileTarget.REVIEWER_PROMPT
    )


def test_workspace_file_locator_validates_and_serializes_existing_values() -> None:
    locator = WorkspaceFileLocator(**_workspace_file_locator_payload())

    assert locator.target == WorkspaceFileTarget.EXECUTOR_PROMPT
    assert locator.source_class == WorkspaceFileSourceClass.PROJECT_INITIAL
    dumped = locator.model_dump(mode="json")
    assert dumped["target"] == "executor_prompt"
    assert dumped["source_class"] == "project_initial"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target", "Executor_Prompt"),
        ("target", "unknown"),
        ("source_class", "Runtime_Dynamic"),
        ("source_class", "unknown"),
    ],
)
def test_workspace_file_locator_rejects_invalid_domains(
    field: str,
    value: str,
) -> None:
    payload = _workspace_file_locator_payload()
    payload[field] = value

    with pytest.raises(ValidationError):
        WorkspaceFileLocator(**payload)


def test_workspace_source_class_selection_uses_workspace_file_enums() -> None:
    workflow = WorkflowPlan(
        name="workspace source classes",
        worktrees={"primary": {"kind": "worktree"}},
        nodes=[
            WorkflowNode(id="input", mode="input", source="{{file:README.md}}"),
            WorkflowNode(
                id="seed",
                mode="sequential",
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="seed")
                ],
            ),
            WorkflowNode(
                id="build",
                mode="sequential",
                needs=["seed"],
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[
                    PromptSegment(
                        role=PromptSegmentRole.SHARED, content="{{file:generated.md}}"
                    )
                ],
            ),
        ],
    )
    input_node, seed_node, build_node = workflow.nodes

    assert (
        locator_source_class(workflow, input_node, WorkspaceFileTarget.INPUT_OUTPUT)
        == WorkspaceFileSourceClass.PROJECT_INITIAL
    )
    assert (
        locator_source_class(
            workflow,
            seed_node,
            WorkspaceFileTarget.REVIEWER_PROMPT,
        )
        == WorkspaceFileSourceClass.RUNTIME_DYNAMIC
    )
    assert (
        locator_source_class(
            workflow,
            build_node,
            WorkspaceFileTarget.EXECUTOR_PROMPT,
        )
        == WorkspaceFileSourceClass.RUNTIME_DYNAMIC
    )


def _workspace_file_locator_payload() -> dict[str, object]:
    reference = TemplateReference(
        raw_token="{{file:README.md}}",
        start=0,
        end=18,
        kind="file",
        key="README.md",
    )
    return {
        "locator_id": "workspace-file-demo",
        "content_ref": "workspace-files/workspace-file-demo.txt",
        "occurrence_id": "build:executor:0:file:README.md",
        "node_id": "build",
        "target": "executor_prompt",
        "source_class": "project_initial",
        "raw_token": reference.raw_token,
        "raw_path": reference.key,
        "source_root": "/repo",
        "source_root_relative_to_project": ".",
        "project_root_relative_to_git_top": ".",
        "git_top_relative_path": "README.md",
        "workspace_relative_path": "README.md",
    }
