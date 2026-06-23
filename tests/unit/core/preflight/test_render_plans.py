from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from rich.console import Console

from crewplane.bootstrap import build_runtime_config_snapshot
from crewplane.core.config import (
    AgentConfig,
    Config,
    IntegrationsConfig,
    IntegrationSpec,
    Settings,
)
from crewplane.core.preflight import (
    PreflightCompileOptions,
    PreflightWorkflowSource,
    compile_preflight_preview,
    render_plans,
)
from crewplane.core.preflight.models import WorkspaceFileTarget
from crewplane.core.preflight.references import TemplateReference
from crewplane.core.prompt_segments import PromptSegment, PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import (
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.version import SCHEMA_VERSION


def _config() -> Config:
    return Config(
        version=SCHEMA_VERSION,
        agents={
            "alpha": AgentConfig(cli_cmd=["mock"]),
            "beta": AgentConfig(cli_cmd=["mock"]),
        },
        settings=Settings(
            integrations=IntegrationsConfig(
                invoker=IntegrationSpec(
                    implementation="mock",
                    options={"output_mode": "echo"},
                ),
                artifacts=IntegrationSpec(
                    implementation="filesystem",
                    options={"allowed_template_paths": [], "log_cli_output": True},
                ),
                ui=IntegrationSpec(implementation="none", options={}),
            )
        ),
    )


def _compile_preview(root: Path, workflow: WorkflowPlan):
    config = _config()
    snapshot = build_runtime_config_snapshot(
        config=config,
        console=Console(file=None),
        no_live=True,
    )
    return compile_preflight_preview(
        source=PreflightWorkflowSource.from_workflow(workflow),
        config=config,
        runtime_snapshot=snapshot.snapshot,
        options=PreflightCompileOptions(
            project_root=root,
            state_dir=root / ".crewplane",
            fingerprint_key_policy="read_only",
        ),
    )


def test_render_token_occurrences_scan_each_segment_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanned_segments: list[str] = []

    def fake_iter_template_references(text: str) -> tuple[TemplateReference, ...]:
        scanned_segments.append(text)
        return (
            TemplateReference(
                raw_token=f"{{{{file:{text}.md}}}}",
                start=0,
                end=len(text),
                kind="file",
                key=f"{text}.md",
            ),
        )

    monkeypatch.setattr(
        render_plans,
        "iter_template_references",
        fake_iter_template_references,
    )
    workflow = WorkflowPlan(
        name="demo",
        nodes=[
            WorkflowNode(
                id="seed",
                mode="input",
                source="{{file:seed.md}}",
            ),
            WorkflowNode(
                id="build",
                mode="sequential",
                providers=[
                    ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR),
                    ProviderSpec(provider="beta", role=ProviderRole.REVIEWER),
                ],
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="shared"),
                    PromptSegment(role=PromptSegmentRole.EXECUTOR, content="executor"),
                    PromptSegment(role=PromptSegmentRole.REVIEWER, content="reviewer"),
                ],
            ),
        ],
    )

    occurrences = list(render_plans.iter_render_token_occurrences(workflow))

    assert scanned_segments == ["shared", "executor", "reviewer"]
    assert [
        (occurrence.target_role, occurrence.segment_index, occurrence.occurrence_id)
        for occurrence in occurrences
    ] == [
        (ProviderRole.EXECUTOR, 0, "build:executor:0:0"),
        (ProviderRole.REVIEWER, 0, "build:reviewer:0:0"),
        (ProviderRole.EXECUTOR, 1, "build:executor:1:0"),
        (ProviderRole.REVIEWER, 2, "build:reviewer:2:0"),
    ]


def test_workspace_file_target_for_role_maps_explicitly() -> None:
    assert (
        render_plans.workspace_file_target_for_role(ProviderRole.EXECUTOR)
        == WorkspaceFileTarget.EXECUTOR_PROMPT
    )
    assert (
        render_plans.workspace_file_target_for_role(ProviderRole.REVIEWER)
        == WorkspaceFileTarget.REVIEWER_PROMPT
    )

    invalid_role = cast(ProviderRole, "author")
    with pytest.raises(ValueError, match="Unsupported render target role"):
        render_plans.workspace_file_target_for_role(invalid_role)


def test_compiled_token_catalog_uses_segment_scoped_occurrence_ids(
    tmp_path: Path,
) -> None:
    (tmp_path / "context.md").write_text("context", encoding="utf-8")
    workflow = WorkflowPlan(
        name="demo",
        nodes=[
            WorkflowNode(
                id="review",
                mode="sequential",
                providers=[
                    ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR),
                    ProviderSpec(provider="beta", role=ProviderRole.REVIEWER),
                ],
                prompt_segments=[
                    PromptSegment(
                        role=PromptSegmentRole.SHARED,
                        content="Shared {{file:context.md}}",
                    ),
                    PromptSegment(
                        role=PromptSegmentRole.EXECUTOR,
                        content="Exec {{file:context.md}}",
                    ),
                    PromptSegment(
                        role=ProviderRole.REVIEWER, content="Review {{file:context.md}}"
                    ),
                ],
            )
        ],
    )

    preview = _compile_preview(tmp_path, workflow)

    assert not preview.diagnostics
    assert [
        entry.occurrence_id
        for entry in preview.token_catalog
        if entry.token_kind == "file"
    ] == [
        "review:executor:0:0",
        "review:executor:1:0",
        "review:reviewer:0:0",
        "review:reviewer:2:0",
    ]
