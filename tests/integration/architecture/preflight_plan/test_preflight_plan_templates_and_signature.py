from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console

from orchestrator_cli.architecture.api_version import EXT_API_VERSION
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
    PreflightExecutionPlan,
    PreflightWorkflowSource,
    compile_preflight_preview,
    load_workflow_source_for_preflight,
)
from orchestrator_cli.core.preflight.runtime_config import CanonicalIntegrationConfig
from orchestrator_cli.core.prompt_segments import PromptSegment
from orchestrator_cli.core.workflow_models import (
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)


def _mock_config() -> Config:
    return Config(
        version="1.0",
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
            api_version=EXT_API_VERSION,
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
        workflow_schema_version=workflow.schema_version,
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


def test_binary_static_file_token_fails_deterministically(tmp_path: Path) -> None:
    binary_file = tmp_path / "payload.bin"
    binary_file.write_bytes(b"\xff\xfe\x00")
    config = _mock_config()
    workflow = WorkflowPlan(
        name="demo",
        nodes=[
            WorkflowNode(
                id="build",
                mode="sequential",
                providers=[ProviderSpec(provider="mock")],
                prompt_segments=[
                    PromptSegment(role="shared", content="{{file:payload.bin}}")
                ],
            )
        ],
    )
    snapshot = build_runtime_config_snapshot(
        config=config,
        workflow_schema_version=workflow.schema_version,
        console=Console(file=None),
        no_live=True,
    )
    preview = compile_preflight_preview(
        source=_source(workflow),
        config=config,
        runtime_snapshot=snapshot.snapshot,
        options=PreflightCompileOptions(
            project_root=tmp_path,
            orchestrator_dir=tmp_path / ".orchestrator",
            fingerprint_key_policy="read_only",
        ),
    )

    assert preview.has_errors()
    assert [diagnostic.code for diagnostic in preview.diagnostics] == ["FILE-ENCODING"]


def test_imported_file_token_resolves_from_imported_module_root(tmp_path: Path) -> None:
    root = tmp_path
    child_dir = root / "child"
    child_dir.mkdir()
    (child_dir / "context.md").write_text("child context", encoding="utf-8")
    (child_dir / "workflow.task.md").write_text(
        "\n".join(
            [
                "---",
                'schema_version: "1.0"',
                "name: Child",
                "nodes:",
                "  - id: build",
                "    mode: sequential",
                "    providers: [mock]",
                "---",
                "",
                "## build",
                "",
                "{{file:context.md}}",
            ]
        ),
        encoding="utf-8",
    )
    root_workflow = root / "root.task.md"
    root_workflow.write_text(
        "\n".join(
            [
                "---",
                'schema_version: "1.0"',
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

    original_cwd = Path.cwd()
    os.chdir(root)
    try:
        source = load_workflow_source_for_preflight(root_workflow, project_root=root)
    finally:
        os.chdir(original_cwd)
    workflow = source.workflow
    config = _mock_config()
    snapshot = build_runtime_config_snapshot(
        config=config,
        workflow_schema_version=workflow.schema_version,
        console=Console(file=None),
        no_live=True,
    )
    preview = compile_preflight_preview(
        source=source,
        config=config,
        runtime_snapshot=snapshot.snapshot,
        options=PreflightCompileOptions(
            project_root=root,
            orchestrator_dir=root / ".orchestrator",
            fingerprint_key_policy="read_only",
        ),
    )

    assert not preview.diagnostics
    assert set(preview.static_file_payloads.values()) == {b"child context"}
    assert all(
        resource.source_root == child_dir.as_posix()
        for resource in preview.static_resources
    )


def test_persisted_plan_keeps_preview_workflow_signature(tmp_path: Path) -> None:
    config = _mock_config()
    workflow = _literal_workflow()
    snapshot = build_runtime_config_snapshot(
        config=config,
        workflow_schema_version=workflow.schema_version,
        console=Console(file=None),
        no_live=True,
    )
    preview = compile_preflight_preview(
        source=_source(workflow),
        config=config,
        runtime_snapshot=snapshot.snapshot,
        options=PreflightCompileOptions(
            project_root=tmp_path,
            orchestrator_dir=tmp_path / ".orchestrator",
            fingerprint_key_policy="read_only",
        ),
    )
    assert preview.workflow_signature is not None

    plan = PreflightExecutionPlan.from_preview(
        preview=preview,
        run_id="run-a",
        run_key_name="demo-run-a",
        context_root="/tmp/demo-run-a",
        manifest_root="/tmp/demo-run-a/manifests",
        created_at=datetime(2026, 6, 3),
    )
    serialized = json.loads(plan.model_dump_json())

    assert plan.workflow_signature == preview.workflow_signature
    assert serialized["plan_schema_version"] == "1.0"
    assert "schema_version" not in serialized
    assert "{{param:" not in json.dumps(serialized)
