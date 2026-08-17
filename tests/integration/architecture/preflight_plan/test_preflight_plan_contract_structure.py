from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from crewplane.architecture.contracts import CanonicalIntegrationConfig
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
    PreflightExecutionPlan,
    PreflightWorkflowSource,
    compile_preflight_preview,
)
from crewplane.core.prompt_segments import PromptSegment, PromptSegmentRole
from crewplane.core.workflow.models import (
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.version import SCHEMA_VERSION
from tests.helpers.resume import make_plan


def _mock_config() -> Config:
    return Config(
        version=SCHEMA_VERSION,
        agents={"mock": AgentConfig(cli_cmd=["mock"])},
        settings=Settings(
            integrations=IntegrationsConfig(
                invoker=IntegrationSpec(
                    implementation="mock",
                    options={
                        "observation_delay_seconds": 0,
                        "output_mode": "echo",
                    },
                ),
                ui=IntegrationSpec(implementation="tmux", options={}),
                artifacts=IntegrationSpec(
                    implementation="filesystem",
                    options={"log_cli_output": True},
                ),
            )
        ),
    )


def _literal_workflow() -> WorkflowPlan:
    return WorkflowPlan(
        name="demo",
        nodes=[
            WorkflowNode(
                id="build",
                mode="sequential",
                providers=[ProviderSpec(provider="mock")],
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="hello")
                ],
            )
        ],
    )


def _source(
    workflow: WorkflowPlan,
    workflow_content: str = "workflow source",
    composed_workflow: dict[str, Any] | None = None,
    node_source_paths: dict[str, Path] | None = None,
    node_source_spans: dict[str, dict[str, int]] | None = None,
    prompt_segment_spans: dict[str, list[dict[str, int]]] | None = None,
) -> PreflightWorkflowSource:
    return PreflightWorkflowSource.from_workflow(
        workflow,
        workflow_content=workflow_content,
        composed_workflow=composed_workflow
        or {
            "schema_version": workflow.schema_version,
            "name": workflow.name,
            "description": workflow.description,
            "inputs": dict(workflow.inputs),
            "nodes": [],
        },
        node_source_paths=node_source_paths,
        node_source_spans=node_source_spans,
        prompt_segment_spans=prompt_segment_spans,
    )


class SensitiveOptionInvokerAdapter:
    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: Mapping[str, Any] | None = None,
    ) -> CanonicalIntegrationConfig:
        raw_options = dict(options or {})
        api_token = raw_options.pop("api_token")
        if raw_options:
            raise ValueError(f"Unsupported options: {sorted(raw_options)}")
        return CanonicalIntegrationConfig(
            implementation=implementation,
            resolved_identity=resolved_identity,
            options={"api_token": api_token},
            sensitive_options=["/api_token"],
            option_scopes={"api_token": "execution"},
        )

    def create_invoker(
        self,
        config: Config,  # noqa: ARG002 - Required by adapter protocol.
        options: Mapping[str, Any] | None = None,  # noqa: ARG002 - Required by adapter protocol.
    ) -> object:
        raise AssertionError("preflight preview must not construct the invoker")


def _compile_signature(
    root: Path,
    no_live: bool,
    settings_update: dict[str, object] | None = None,
    workflow: WorkflowPlan | None = None,
) -> str:
    config = _mock_config()
    if settings_update:
        assert config.settings is not None
        config.settings = config.settings.model_copy(update=settings_update)
    selected_workflow = workflow or _literal_workflow()
    snapshot = build_runtime_config_snapshot(
        config=config,
        console=Console(file=None),
        no_live=no_live,
    )
    preview = compile_preflight_preview(
        source=_source(selected_workflow),
        config=config,
        runtime_snapshot=snapshot.snapshot,
        options=PreflightCompileOptions(
            project_root=root,
            state_dir=root / ".crewplane",
            fingerprint_key_policy="read_only",
        ),
    )
    assert not preview.diagnostics
    assert preview.workflow_signature is not None
    return preview.workflow_signature


def test_no_live_is_excluded_from_workflow_signature(tmp_path: Path) -> None:
    assert _compile_signature(tmp_path, no_live=False) == _compile_signature(
        tmp_path,
        no_live=True,
    )


@pytest.mark.parametrize(
    ("setting_name", "value"),
    [
        ("log_level", "DEBUG"),
        ("max_audit_rounds", 99),
        ("sequential_consensus_on_exhaustion", "fatal"),
    ],
)
def test_ineffective_settings_are_excluded_from_workflow_signature(
    tmp_path: Path,
    setting_name: str,
    value: object,
) -> None:
    baseline = _compile_signature(tmp_path, no_live=True)
    changed = _compile_signature(
        tmp_path,
        no_live=True,
        settings_update={setting_name: value},
    )

    assert changed == baseline


def test_effective_execution_setting_changes_workflow_signature(
    tmp_path: Path,
) -> None:
    baseline = _compile_signature(tmp_path, no_live=True)
    changed = _compile_signature(
        tmp_path,
        no_live=True,
        settings_update={"max_concurrent_nodes": 1},
    )

    assert changed != baseline


def test_consensus_policy_changes_signature_when_review_loop_is_active(
    tmp_path: Path,
) -> None:
    workflow = WorkflowPlan(
        name="review",
        nodes=[
            WorkflowNode(
                id="review",
                mode="sequential",
                providers=[
                    ProviderSpec(
                        provider="mock",
                        role="executor",
                    ),
                    ProviderSpec(
                        provider="mock",
                        role="reviewer",
                    ),
                ],
                prompt_segments=[
                    PromptSegment(
                        role=PromptSegmentRole.SHARED,
                        content="review",
                    )
                ],
            )
        ],
    )

    baseline = _compile_signature(tmp_path, no_live=True, workflow=workflow)
    changed = _compile_signature(
        tmp_path,
        no_live=True,
        settings_update={"sequential_consensus_on_exhaustion": "fatal"},
        workflow=workflow,
    )

    assert changed != baseline


def test_mock_execution_options_change_runtime_signature() -> None:
    first_config = _mock_config()
    second_config = _mock_config()
    assert first_config.settings is not None
    assert second_config.settings is not None
    first_config.settings.integrations.invoker.options["seed"] = 1
    second_config.settings.integrations.invoker.options["seed"] = 2

    first_snapshot = build_runtime_config_snapshot(
        config=first_config,
        console=Console(file=None),
        no_live=True,
    ).snapshot
    second_snapshot = build_runtime_config_snapshot(
        config=second_config,
        console=Console(file=None),
        no_live=True,
    ).snapshot

    assert (
        first_snapshot.effective_runtime_config_signature
        != second_snapshot.effective_runtime_config_signature
    )


def test_compiled_plan_persists_execution_contract_metadata(tmp_path: Path) -> None:
    workflow_file = tmp_path / ".crewplane" / "workflows" / "demo.task.md"
    workflow_file.parent.mkdir(parents=True)
    workflow_file.write_text("workflow source", encoding="utf-8")
    config = _mock_config()
    workflow = WorkflowPlan(
        name="demo",
        nodes=[
            WorkflowNode(
                id="build",
                mode="sequential",
                providers=[ProviderSpec(provider="mock")],
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="build")
                ],
            ),
            WorkflowNode(
                id="review",
                mode="sequential",
                needs=["build"],
                providers=[ProviderSpec(provider="mock")],
                prompt_segments=[
                    PromptSegment(
                        role=PromptSegmentRole.SHARED, content="Review {{build.output}}"
                    )
                ],
            ),
        ],
    )
    snapshot = build_runtime_config_snapshot(
        config=config,
        console=Console(file=None),
        no_live=True,
    )

    preview = compile_preflight_preview(
        source=_source(
            workflow,
            workflow_content=workflow_file.read_text(encoding="utf-8"),
            node_source_paths={"review": workflow_file},
            node_source_spans={"review": {"start_line": 10, "end_line": 14}},
            prompt_segment_spans={"review": [{"start_line": 12, "end_line": 13}]},
        ),
        config=config,
        runtime_snapshot=snapshot.snapshot,
        options=PreflightCompileOptions(
            project_root=tmp_path,
            state_dir=tmp_path / ".crewplane",
            fingerprint_key_policy="read_only",
        ),
    )

    assert not preview.diagnostics
    review_node = next(node for node in preview.nodes if node.id == "review")
    render_plan = next(
        plan for plan in preview.render_plans if plan.render_plan_id == "review"
    )
    token = next(entry for entry in preview.token_catalog if entry.token_kind == "node")
    token_edge = next(edge for edge in preview.dependency_graph if edge.artifact_key)

    assert review_node.module_id == ".crewplane/workflows/demo.task.md"
    assert review_node.artifact_contract.output_path == "review-result.md"
    assert render_plan.node_id == "review"
    assert render_plan.source_file == workflow_file.as_posix()
    assert render_plan.source_span == {"start_line": 10, "end_line": 14}
    assert review_node.source_span == {"start_line": 10, "end_line": 14}
    assert render_plan.template_hash is not None
    assert token.canonical_locator == "build.output"
    assert token.resolved["kind"] == "runtime_locator_lookup"
    assert token.token_raw_span == {"start": 7, "end": 23}
    assert token.source_span == {
        "start_line": 12,
        "start_column": 7,
        "end_line": 12,
        "end_column": 23,
    }
    assert token_edge.first_token_signature == token.signature
    assert token_edge.target_locator == "build.output"


@pytest.mark.parametrize(
    ("source_lineage_producer", "target_worktree", "message"),
    [
        (True, "secondary", "same logical worktree"),
        (False, "primary", "lineage producer"),
    ],
)
def test_serialized_plan_rejects_invalid_workspace_source_lineage(
    source_lineage_producer: bool,
    target_worktree: str,
    message: str,
) -> None:
    payload = make_plan().model_dump(mode="json")
    payload["nodes"][0]["workspace_policy"] = {
        "enabled": True,
        "logical_worktree_name": "primary",
        "declaration_kind": "worktree",
        "source_kind": "project",
        "materialization": "worktree_checkout",
        "writable": True,
        "lineage_producer": source_lineage_producer,
    }
    payload["nodes"][1]["workspace_policy"] = {
        "enabled": True,
        "logical_worktree_name": target_worktree,
        "declaration_kind": "worktree",
        "source_kind": "node",
        "source_node_id": "a",
        "materialization": "worktree_checkout",
        "writable": True,
        "lineage_producer": True,
    }

    with pytest.raises(ValueError, match=message):
        PreflightExecutionPlan.model_validate_json(json.dumps(payload))


def test_serialized_plan_rejects_project_source_lineage_rollback() -> None:
    payload = make_plan().model_dump(mode="json")
    payload["nodes"][0]["workspace_policy"] = _worktree_policy("primary")
    payload["nodes"][1]["workspace_policy"] = _worktree_policy("primary")

    with pytest.raises(ValueError, match="latest same-worktree lineage source 'a'"):
        PreflightExecutionPlan.model_validate_json(json.dumps(payload))


def test_serialized_plan_rejects_direct_cross_worktree_dependency() -> None:
    payload = make_plan().model_dump(mode="json")
    payload["nodes"][0]["workspace_policy"] = _worktree_policy("primary")
    payload["nodes"][1]["workspace_policy"] = _worktree_policy("secondary")

    with pytest.raises(ValueError, match="same logical worktree"):
        PreflightExecutionPlan.model_validate_json(json.dumps(payload))


def test_serialized_plan_rejects_older_transitive_lineage_source() -> None:
    payload = make_plan().model_dump(mode="json")
    payload["nodes"][0]["workspace_policy"] = _worktree_policy("primary")
    payload["nodes"][1]["workspace_policy"] = _worktree_policy(
        "primary",
        source_node_id="a",
    )
    third_node = deepcopy(payload["nodes"][1])
    third_node.update(
        {
            "id": "c",
            "render_plan_id": "c",
            "dependencies": ["b"],
            "artifact_contract": {
                "stage_path": "c",
                "output_path": "c-result.md",
                "findings_path": None,
                "log_path": "c/logs",
                "result_path": "c-result.md",
            },
        }
    )
    payload["nodes"].append(third_node)
    payload["execution_order"].append("c")
    third_render_plan = deepcopy(payload["render_plans"][1])
    third_render_plan.update({"render_plan_id": "c", "node_id": "c"})
    payload["render_plans"].append(third_render_plan)
    payload["dependency_graph"].append(
        {
            "source_node": "b",
            "target_node": "c",
            "artifact_name": "output",
            "dependency_signature": "b-to-c",
            "target_locator": "b.output",
            "artifact_key": "output",
        }
    )

    with pytest.raises(ValueError, match="latest same-worktree lineage source 'b'"):
        PreflightExecutionPlan.model_validate_json(json.dumps(payload))


def _worktree_policy(
    logical_worktree_name: str,
    source_node_id: str | None = None,
) -> dict[str, object]:
    return {
        "enabled": True,
        "logical_worktree_name": logical_worktree_name,
        "declaration_kind": "worktree",
        "source_kind": "node" if source_node_id is not None else "project",
        "source_node_id": source_node_id,
        "materialization": "worktree_checkout",
        "writable": True,
        "lineage_producer": True,
    }
