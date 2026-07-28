from pathlib import Path

from rich.console import Console

from crewplane.bootstrap import build_runtime_config_snapshot
from crewplane.cli.run.preflight import run_reasoning_control_errors
from crewplane.core.config import (
    AgentConfig,
    Config,
    IntegrationsConfig,
    IntegrationSpec,
    Settings,
)
from crewplane.core.preflight import (
    PreflightCompilationPreview,
    PreflightCompileOptions,
    PreflightWorkflowSource,
    compile_preflight_preview,
)
from crewplane.core.preflight.secrets import FingerprintKeyPolicy
from crewplane.core.prompt_segments import PromptSegment, PromptSegmentRole
from crewplane.core.workflow.models import (
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.version import SCHEMA_VERSION


def _config() -> Config:
    return Config(
        version=SCHEMA_VERSION,
        agents={"alpha": AgentConfig(cli_cmd=["mock"])},
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


def _preview(
    workflow: WorkflowPlan,
    root: Path,
    environment: dict[str, str] | None = None,
    runtime_variables: dict[str, str] | None = None,
    fingerprint_key_policy: FingerprintKeyPolicy = "read_only",
) -> PreflightCompilationPreview:
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
            fingerprint_key_policy=fingerprint_key_policy,
            environment=environment,
            runtime_variables=runtime_variables or {},
        ),
    )


def test_preflight_preview_reports_missing_env_before_runtime(tmp_path: Path) -> None:
    workflow = WorkflowPlan(
        name="demo",
        nodes=[
            WorkflowNode(
                id="build",
                mode="sequential",
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[
                    PromptSegment(
                        role=PromptSegmentRole.SHARED, content="{{env:MISSING_KEY}}"
                    )
                ],
            )
        ],
    )

    preview = _preview(workflow, tmp_path)

    assert preview.workflow_signature is None
    assert [
        (diagnostic.code, diagnostic.phase) for diagnostic in preview.diagnostics
    ] == [("TEMPLATE-VALUE", "env_policy")]
    assert preview.render_plans == []
    assert preview.token_catalog == []


def test_preflight_stops_before_static_policies_after_reference_errors(
    tmp_path: Path,
) -> None:
    (tmp_path / "context.md").write_text("must not be read", encoding="utf-8")
    workflow = WorkflowPlan(
        name="demo",
        nodes=[
            WorkflowNode(
                id="build",
                mode="sequential",
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[
                    PromptSegment(
                        role="shared",
                        content=(
                            "{{missing.output}} {{file:context.md}} "
                            "{{env:SHOULD_NOT_BE_READ}}"
                        ),
                    )
                ],
            )
        ],
    )

    preview = _preview(workflow, tmp_path, environment={})

    assert preview.workflow_signature is None
    assert [
        (diagnostic.code, diagnostic.phase) for diagnostic in preview.diagnostics
    ] == [("PREFLIGHT-VALIDATION", "reference")]
    assert preview.diagnostics[0].metadata["workflow_code"] == "WORKFLOW-TEMPLATE"
    assert preview.static_resources == []
    assert preview.static_file_payloads == {}
    assert preview.render_plans == []
    assert preview.token_catalog == []


def test_file_policy_runs_before_authored_env_tokens(tmp_path: Path) -> None:
    workflow = WorkflowPlan(
        name="demo",
        nodes=[
            WorkflowNode(
                id="build",
                mode="sequential",
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[
                    PromptSegment(
                        role="shared",
                        content=("{{env:SHOULD_NOT_BE_READ}} {{file:missing.md}}"),
                    )
                ],
            )
        ],
    )

    preview = _preview(workflow, tmp_path, environment={})

    assert [
        (diagnostic.code, diagnostic.phase) for diagnostic in preview.diagnostics
    ] == [("FILE-POLICY", "file_policy")]
    assert preview.render_plans == []
    assert preview.token_catalog == []


def test_env_policy_runs_before_authored_var_tokens(tmp_path: Path) -> None:
    workflow = WorkflowPlan(
        name="demo",
        nodes=[
            WorkflowNode(
                id="build",
                mode="sequential",
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[
                    PromptSegment(
                        role="shared",
                        content=("{{var:SHOULD_NOT_BE_READ}} {{env:MISSING_KEY}}"),
                    )
                ],
            )
        ],
    )

    preview = _preview(workflow, tmp_path, environment={}, runtime_variables={})

    assert [
        (diagnostic.code, diagnostic.phase) for diagnostic in preview.diagnostics
    ] == [("TEMPLATE-VALUE", "env_policy")]
    assert preview.render_plans == []
    assert preview.token_catalog == []


def test_env_policy_failure_does_not_publish_fingerprint_key(
    tmp_path: Path,
) -> None:
    workflow = WorkflowPlan(
        name="demo",
        nodes=[
            WorkflowNode(
                id="build",
                mode="sequential",
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[
                    PromptSegment(
                        role="shared",
                        content="{{env:API_TOKEN}} {{env:MISSING_KEY}}",
                    )
                ],
            )
        ],
    )

    preview = _preview(
        workflow,
        tmp_path,
        environment={"API_TOKEN": "super-secret"},
        fingerprint_key_policy="persist_if_needed",
    )

    assert [
        (diagnostic.code, diagnostic.phase) for diagnostic in preview.diagnostics
    ] == [("TEMPLATE-VALUE", "env_policy")]
    assert preview.workflow_signature is None
    assert preview.value_fingerprints == []
    assert preview.fingerprint_metadata["fingerprint_key_persisted"] is False
    assert not (tmp_path / ".crewplane" / "preflight" / "fingerprint.key").exists()


def test_var_policy_failure_does_not_publish_fingerprint_key(
    tmp_path: Path,
) -> None:
    workflow = WorkflowPlan(
        name="demo",
        nodes=[
            WorkflowNode(
                id="build",
                mode="sequential",
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[
                    PromptSegment(
                        role="shared",
                        content="{{var:private_key}} {{var:MISSING_KEY}}",
                    )
                ],
            )
        ],
    )

    preview = _preview(
        workflow,
        tmp_path,
        runtime_variables={"private_key": "super-secret"},
        fingerprint_key_policy="persist_if_needed",
    )

    assert [
        (diagnostic.code, diagnostic.phase) for diagnostic in preview.diagnostics
    ] == [("TEMPLATE-VALUE", "var_policy")]
    assert preview.workflow_signature is None
    assert preview.value_fingerprints == []
    assert preview.fingerprint_metadata["fingerprint_key_persisted"] is False
    assert not (tmp_path / ".crewplane" / "preflight" / "fingerprint.key").exists()


def test_successful_preflight_preview_builds_execution_contract(
    tmp_path: Path,
) -> None:
    workflow = WorkflowPlan(
        name="demo",
        nodes=[
            WorkflowNode(
                id="build",
                mode="sequential",
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Build")
                ],
            )
        ],
    )

    preview = _preview(workflow, tmp_path)

    assert not preview.diagnostics
    assert preview.workflow_signature is not None
    assert preview.execution_order == ["build"]
    assert preview.nodes[0].provider_records[0].agent_config_key == "alpha"


def test_preflight_records_reasoning_and_changes_execution_identity(
    tmp_path: Path,
) -> None:
    base_node = WorkflowNode(
        id="build",
        mode="sequential",
        providers=[ProviderSpec(provider="alpha")],
        prompt_segments=[PromptSegment(role=PromptSegmentRole.SHARED, content="Build")],
    )
    default_preview = _preview(
        WorkflowPlan(name="demo", nodes=[base_node]),
        tmp_path,
    )
    reasoning_preview = _preview(
        WorkflowPlan(
            name="demo",
            nodes=[
                base_node.model_copy(
                    update={
                        "providers": [ProviderSpec(provider="alpha", reasoning="high")]
                    }
                )
            ],
        ),
        tmp_path,
    )

    provider = reasoning_preview.nodes[0].provider_records[0]
    assert provider.requested_reasoning == "high"
    assert default_preview.nodes[0].provider_records[0].requested_reasoning is None
    assert reasoning_preview.workflow_signature != default_preview.workflow_signature


def test_reasoning_control_rejects_non_cli_invoker() -> None:
    workflow = WorkflowPlan(
        name="demo",
        nodes=[
            WorkflowNode(
                id="build",
                mode="sequential",
                providers=[ProviderSpec(provider="alpha", reasoning="high")],
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Build")
                ],
            )
        ],
    )

    errors = run_reasoning_control_errors(workflow, _config())

    assert errors == (
        "workflow 'demo' -> node 'build' -> provider 'alpha': first-class "
        "reasoning requires the built-in CLI invoker.",
    )


def test_reasoning_control_checks_relative_claude_settings_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "claude-settings.json").write_text(
        '{"effortLevel": "low"}',
        encoding="utf-8",
    )
    workflow = WorkflowPlan(
        name="demo",
        nodes=[
            WorkflowNode(
                id="build",
                mode="sequential",
                providers=[ProviderSpec(provider="alpha", reasoning="high")],
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Build")
                ],
            )
        ],
    )
    config = Config(
        version=SCHEMA_VERSION,
        agents={
            "alpha": AgentConfig(
                cli_cmd=["claude", "--settings", "claude-settings.json"],
                provider_kind="claude",
            )
        },
        settings=Settings(
            integrations=IntegrationsConfig(
                invoker=IntegrationSpec(implementation="cli", options={}),
                artifacts=IntegrationSpec(
                    implementation="filesystem",
                    options={"allowed_template_paths": [], "log_cli_output": True},
                ),
                ui=IntegrationSpec(implementation="none", options={}),
            )
        ),
    )

    errors = run_reasoning_control_errors(workflow, config, tmp_path)

    assert errors == (
        "workflow 'demo' -> node 'build' -> provider 'alpha': --settings "
        "effortLevel conflicts with the workflow reasoning request.",
    )


def test_preflight_preview_warns_on_argv_prompt_transport(
    tmp_path: Path,
) -> None:
    workflow = WorkflowPlan(
        name="demo",
        nodes=[
            WorkflowNode(
                id="build",
                mode="sequential",
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="build")
                ],
            )
        ],
    )
    config = _config()
    config.agents["alpha"].prompt_transport = "argv"
    config.agents["alpha"].prompt_transport_arg = "--prompt"
    config_snapshot = build_runtime_config_snapshot(
        config=config,
        console=Console(file=None),
        no_live=True,
    )
    preview = compile_preflight_preview(
        source=PreflightWorkflowSource.from_workflow(workflow),
        config=config,
        runtime_snapshot=config_snapshot.snapshot,
        options=PreflightCompileOptions(
            project_root=tmp_path,
            state_dir=tmp_path / ".crewplane",
            fingerprint_key_policy="read_only",
            environment=None,
            runtime_variables={},
        ),
    )

    self_diagnostics = [
        (diagnostic.code, diagnostic.severity, diagnostic.phase)
        for diagnostic in preview.diagnostics
        if diagnostic.code == "PROVIDER-CONFIG"
    ]
    assert ("PROVIDER-CONFIG", "warning", "provider") in self_diagnostics
