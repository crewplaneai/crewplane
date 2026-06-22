from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rich.console import Console

from orchestrator_cli.architecture.contracts import CanonicalIntegrationConfig
from orchestrator_cli.bootstrap import build_runtime_config_snapshot
from orchestrator_cli.core.config import (
    AgentConfig,
    Config,
    IntegrationsConfig,
    IntegrationSpec,
    Settings,
)
from orchestrator_cli.core.preflight import (
    PreflightCompileOptions,
    PreflightWorkflowSource,
    compile_preflight_preview,
)
from orchestrator_cli.core.prompt_segments import PromptSegment
from orchestrator_cli.core.workflow_models import (
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from orchestrator_cli.version import SCHEMA_VERSION


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
                    options={"allowed_template_paths": [], "log_cli_output": True},
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
                prompt_segments=[PromptSegment(role="shared", content="hello")],
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
            sensitive_options=["api_token"],
            option_scopes={"api_token": "execution"},
        )

    def create_invoker(
        self,
        config: Config,  # noqa: ARG002 - Required by adapter protocol.
        options: Mapping[str, Any] | None = None,  # noqa: ARG002 - Required by adapter protocol.
    ) -> object:
        raise AssertionError("preflight preview must not construct the invoker")


def _compile_signature(root: Path, no_live: bool) -> str:
    config = _mock_config()
    workflow = _literal_workflow()
    snapshot = build_runtime_config_snapshot(
        config=config,
        console=Console(file=None),
        no_live=no_live,
    )
    preview = compile_preflight_preview(
        source=_source(workflow),
        config=config,
        runtime_snapshot=snapshot.snapshot,
        options=PreflightCompileOptions(
            project_root=root,
            orchestrator_dir=root / ".orchestrator",
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


def test_runtime_execution_modules_do_not_consume_workflow_model_shims() -> None:
    runtime_dir = Path("src/orchestrator_cli/runtime/execution")
    forbidden_terms = (
        "orchestrator_cli.core.workflow_models",
        "WorkflowNode",
        "ProviderSpec",
        "workflow_node_from_plan_node",
        "provider_spec_from_record",
    )

    offenders: list[str] = []
    for path in runtime_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for term in forbidden_terms:
            if term in text:
                offenders.append(f"{path}:{term}")

    assert not offenders


def test_runtime_execution_modules_do_not_consume_runtime_config() -> None:
    runtime_dir = Path("src/orchestrator_cli/runtime/execution")
    forbidden_terms = (
        "from orchestrator_cli.core.config import Config",
        "config: Config",
        "config.settings",
        "config.agents",
        "request.config",
        "context.config",
    )

    offenders: list[str] = []
    for path in runtime_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for term in forbidden_terms:
            if term in text:
                offenders.append(f"{path}:{term}")

    assert not offenders


def test_cli_uses_preflight_runner_for_workflow_source_loading() -> None:
    app_source = Path("src/orchestrator_cli/cli/app.py").read_text(encoding="utf-8")
    runner_source = Path("src/orchestrator_cli/core/preflight/runner.py").read_text(
        encoding="utf-8"
    )
    compiler_source = Path("src/orchestrator_cli/core/preflight/compiler.py").read_text(
        encoding="utf-8"
    )

    assert "load_tasks_with_sources" not in app_source
    assert "validate_workflow_plan" not in app_source
    assert "load_workflow_source_for_preflight" in app_source
    assert "load_tasks_with_sources" not in runner_source
    assert "validate_workflow_plan" not in runner_source
    assert "validate_workflow_plan" not in compiler_source
    assert "def compile_preflight_preview(\n    workflow" not in compiler_source


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
    workflow_file = tmp_path / ".orchestrator" / "workflows" / "demo.task.md"
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
                prompt_segments=[PromptSegment(role="shared", content="build")],
            ),
            WorkflowNode(
                id="review",
                mode="sequential",
                needs=["build"],
                providers=[ProviderSpec(provider="mock")],
                prompt_segments=[
                    PromptSegment(role="shared", content="Review {{build.output}}")
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
            orchestrator_dir=tmp_path / ".orchestrator",
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

    assert review_node.module_id == ".orchestrator/workflows/demo.task.md"
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
