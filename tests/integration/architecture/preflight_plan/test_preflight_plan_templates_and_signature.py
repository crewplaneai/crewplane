from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
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
    PreflightCompilationPreview,
    PreflightCompileOptions,
    PreflightExecutionPlan,
    PreflightWorkflowSource,
    compile_preflight_preview,
    load_workflow_source_for_preflight,
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


def test_imported_file_token_resolves_from_project_root(tmp_path: Path) -> None:
    root = tmp_path
    child_dir = root / "child"
    root_context = root / "docs" / "context.md"
    child_context = child_dir / "docs" / "context.md"
    root_context.parent.mkdir()
    child_context.parent.mkdir(parents=True)
    root_context.write_text("project context", encoding="utf-8")
    child_context.write_text("child context", encoding="utf-8")
    (child_dir / "workflow.task.md").write_text(
        "\n".join(
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Child",
                "nodes:",
                "  - id: build",
                "    mode: sequential",
                "    providers: [mock]",
                "---",
                "",
                "## build",
                "",
                "{{file:docs/context.md}}",
            ]
        ),
        encoding="utf-8",
    )
    root_workflow = root / "root.task.md"
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

    original_cwd = Path.cwd()
    os.chdir(root)
    try:
        source = load_workflow_source_for_preflight(root_workflow, project_root=root)
    finally:
        os.chdir(original_cwd)
    config = _mock_config()
    snapshot = build_runtime_config_snapshot(
        config=config,
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
    assert set(preview.static_file_payloads.values()) == {b"project context"}
    assert all(
        resource.source_root == root.as_posix() for resource in preview.static_resources
    )


def test_persisted_plan_keeps_preview_workflow_signature(tmp_path: Path) -> None:
    config = _mock_config()
    workflow = _literal_workflow()
    snapshot = build_runtime_config_snapshot(
        config=config,
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
        project_root=tmp_path.as_posix(),
        context_root="/tmp/demo-run-a",
        manifest_root="/tmp/demo-run-a/manifests",
        created_at=datetime(2026, 6, 3),
    )
    serialized = json.loads(plan.model_dump_json())

    assert plan.workflow_signature == preview.workflow_signature
    assert serialized["plan_schema_version"] == SCHEMA_VERSION
    assert "schema_version" not in serialized
    assert serialized["runtime_config_snapshot"]["schema_version"] == SCHEMA_VERSION
    assert serialized["fingerprint_metadata"]["payload_version"] == "1"
    assert "{{param:" not in json.dumps(serialized)


def _persisted_plan_payload(root: Path) -> dict[str, Any]:
    config = _mock_config()
    workflow = _literal_workflow()
    snapshot = build_runtime_config_snapshot(
        config=config,
        console=Console(file=None),
        no_live=True,
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
    plan = PreflightExecutionPlan.from_preview(
        preview=preview,
        run_id="run-a",
        run_key_name="demo-run-a",
        project_root=root.as_posix(),
        context_root="/tmp/demo-run-a",
        manifest_root="/tmp/demo-run-a/manifests",
        created_at=datetime(2026, 6, 3),
    )
    return plan.model_dump(mode="python")


def test_preflight_preview_rejects_unsupported_plan_schema_version() -> None:
    with pytest.raises(
        ValidationError,
        match="Unsupported preflight plan schema version '99.0'",
    ):
        PreflightCompilationPreview(plan_schema_version="99.0")


def test_preflight_execution_plan_rejects_unsupported_plan_schema_version(
    tmp_path: Path,
) -> None:
    payload = _persisted_plan_payload(tmp_path)
    payload["plan_schema_version"] = "99.0"

    with pytest.raises(
        ValidationError,
        match="Unsupported preflight plan schema version '99.0'",
    ):
        PreflightExecutionPlan(**payload)


def test_preflight_execution_plan_rejects_legacy_runtime_snapshot_fields(
    tmp_path: Path,
) -> None:
    payload = _persisted_plan_payload(tmp_path)
    runtime_snapshot = dict(payload["runtime_config_snapshot"])
    runtime_snapshot.pop("schema_version")
    runtime_snapshot["config_schema_version"] = SCHEMA_VERSION
    runtime_snapshot["workflow_schema_version"] = SCHEMA_VERSION
    payload["runtime_config_snapshot"] = runtime_snapshot

    with pytest.raises(
        ValidationError,
        match="Unsupported legacy preflight plan runtime config snapshot field",
    ):
        PreflightExecutionPlan(**payload)


def test_preflight_execution_plan_requires_current_runtime_snapshot_marker(
    tmp_path: Path,
) -> None:
    payload = _persisted_plan_payload(tmp_path)
    runtime_snapshot = dict(payload["runtime_config_snapshot"])
    runtime_snapshot.pop("schema_version")
    payload["runtime_config_snapshot"] = runtime_snapshot

    with pytest.raises(
        ValidationError,
        match="must include 'schema_version'",
    ):
        PreflightExecutionPlan(**payload)


def test_preflight_execution_plan_rejects_runtime_snapshot_schema_mismatch(
    tmp_path: Path,
) -> None:
    payload = _persisted_plan_payload(tmp_path)
    runtime_snapshot = dict(payload["runtime_config_snapshot"])
    runtime_snapshot["schema_version"] = "0.9"
    payload["runtime_config_snapshot"] = runtime_snapshot

    with pytest.raises(
        ValidationError,
        match="runtime config snapshot schema_version must be",
    ):
        PreflightExecutionPlan(**payload)


def test_preflight_execution_plan_rejects_legacy_integration_api_version(
    tmp_path: Path,
) -> None:
    payload = _persisted_plan_payload(tmp_path)
    runtime_snapshot = dict(payload["runtime_config_snapshot"])
    invoker = dict(runtime_snapshot["invoker"])
    invoker["api_version"] = "1"
    runtime_snapshot["invoker"] = invoker
    payload["runtime_config_snapshot"] = runtime_snapshot

    with pytest.raises(
        ValidationError,
        match="runtime config 'invoker' integration field",
    ):
        PreflightExecutionPlan(**payload)


def test_preflight_execution_plan_rejects_legacy_fingerprint_metadata_field(
    tmp_path: Path,
) -> None:
    payload = _persisted_plan_payload(tmp_path)
    payload["fingerprint_metadata"] = {"schema_version": "1"}

    with pytest.raises(
        ValidationError,
        match="Unsupported legacy preflight plan fingerprint metadata field",
    ):
        PreflightExecutionPlan(**payload)


def test_preflight_execution_plan_rejects_fingerprint_metadata_version_mismatch(
    tmp_path: Path,
) -> None:
    payload = _persisted_plan_payload(tmp_path)
    payload["fingerprint_metadata"] = {"payload_version": "0"}

    with pytest.raises(
        ValidationError,
        match="fingerprint metadata payload_version must be",
    ):
        PreflightExecutionPlan(**payload)


def test_preflight_execution_plan_rejects_legacy_value_fingerprint_field(
    tmp_path: Path,
) -> None:
    payload = _persisted_plan_payload(tmp_path)
    payload["value_fingerprints"] = [
        {
            "fingerprint": "abc",
            "fingerprint_schema_version": "1",
            "key": "API_TOKEN",
            "kind": "env",
            "sensitive": "true",
        }
    ]

    with pytest.raises(
        ValidationError,
        match="Unsupported legacy preflight plan value fingerprint",
    ):
        PreflightExecutionPlan(**payload)


def test_preflight_execution_plan_rejects_value_fingerprint_version_mismatch(
    tmp_path: Path,
) -> None:
    payload = _persisted_plan_payload(tmp_path)
    payload["value_fingerprints"] = [
        {
            "fingerprint": "abc",
            "fingerprint_payload_version": "0",
            "key": "API_TOKEN",
            "kind": "env",
            "sensitive": "true",
        }
    ]

    with pytest.raises(
        ValidationError,
        match="value fingerprint at index 0 payload version must be",
    ):
        PreflightExecutionPlan(**payload)
