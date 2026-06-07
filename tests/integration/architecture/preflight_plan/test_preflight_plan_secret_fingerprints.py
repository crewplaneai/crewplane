from __future__ import annotations

import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console

import orchestrator_cli.core.preflight.secrets as preflight_secrets
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
    FingerprintKeyProvider,
    PreflightCompileOptions,
    PreflightExecutionPlan,
    PreflightWorkflowSource,
    compile_preflight_preview,
    signature_for_payload,
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


def test_sensitive_config_values_are_hmac_fingerprinted_and_redacted(
    tmp_path: Path,
) -> None:
    workflow = _literal_workflow()

    def compile_with_secret(secret_value: str, split_arg: bool = False) -> str:
        config = _mock_config()
        expected_sensitive_path = "agents.mock.extra_args.0"
        if split_arg:
            config.agents["mock"].extra_args = ["--api-key", secret_value]
            expected_sensitive_path = "agents.mock.extra_args.1"
        else:
            config.agents["mock"].extra_args = [f"--api-key={secret_value}"]
        raw_agent_config_signature = signature_for_payload(
            config.agents["mock"].model_dump(mode="json", exclude_none=True)
        )
        snapshot = build_runtime_config_snapshot(
            config=config,
            workflow_schema_version=workflow.schema_version,
            console=Console(file=None),
            no_live=True,
        )
        assert secret_value not in snapshot.snapshot.model_dump_json()
        assert "--api-key=" not in snapshot.snapshot.model_dump_json()
        preview = compile_preflight_preview(
            source=_source(workflow),
            config=config,
            runtime_snapshot=snapshot.snapshot,
            options=PreflightCompileOptions(
                project_root=tmp_path,
                orchestrator_dir=tmp_path / ".orchestrator",
                fingerprint_key_policy="persist_if_needed",
            ),
        )
        assert not preview.diagnostics
        assert preview.workflow_signature is not None
        serialized_preview = preview.model_dump_json()
        assert raw_agent_config_signature not in serialized_preview
        assert (
            preview.nodes[0].provider_records[0].agent_config_signature
            != raw_agent_config_signature
        )
        assert secret_value not in serialized_preview
        assert "--api-key=" not in serialized_preview
        plan = PreflightExecutionPlan.from_preview(
            preview=preview,
            run_id="run",
            run_key_name="demo-run",
            context_root="/tmp/demo-run",
            manifest_root="/tmp/demo-run/manifests",
            created_at=datetime(2026, 6, 3),
        )
        serialized_plan = plan.model_dump_json()
        assert "runtime_agent_configs" not in serialized_plan
        assert "runtime_agent_config_signatures" not in serialized_plan
        assert raw_agent_config_signature not in serialized_plan
        assert secret_value not in serialized_plan
        assert "--api-key=" not in serialized_plan
        redacted_extra_arg = plan.runtime_config_snapshot["agents"]["mock"][
            "extra_args"
        ][int(expected_sensitive_path.rsplit(".", 1)[-1])]
        assert redacted_extra_arg["redacted"] is True
        assert redacted_extra_arg["value_handle"] == f"config:{expected_sensitive_path}"
        assert preview.secret_context.get(redacted_extra_arg["value_handle"]) == (
            secret_value if split_arg else f"--api-key={secret_value}"
        )
        assert plan.runtime_config_snapshot["sensitive_config_paths"] == [
            expected_sensitive_path
        ]
        fingerprints = plan.runtime_config_snapshot["config_fingerprints"]
        assert len(fingerprints) == 1
        assert fingerprints[0]["path"] == expected_sensitive_path
        assert len(fingerprints[0]["fingerprint"]) == 64
        return preview.workflow_signature

    assert compile_with_secret("first-secret") != compile_with_secret("second-secret")
    assert compile_with_secret("split-first", split_arg=True) != compile_with_secret(
        "split-second",
        split_arg=True,
    )
    key_path = tmp_path / ".orchestrator" / "preflight" / "fingerprint.key"
    assert key_path.exists()
    assert key_path.stat().st_size == 32


def test_sensitive_adapter_options_are_hmac_fingerprinted_and_redacted(
    tmp_path: Path,
) -> None:
    workflow = _literal_workflow()

    def compile_with_token(token_value: object) -> str:
        config = _mock_config()
        assert config.settings is not None
        config.settings.integrations.invoker = IntegrationSpec(
            implementation=f"{__name__}:SensitiveOptionInvokerAdapter",
            options={"api_token": token_value},
        )
        snapshot = build_runtime_config_snapshot(
            config=config,
            workflow_schema_version=workflow.schema_version,
            console=Console(file=None),
            no_live=True,
        )
        serialized_snapshot = snapshot.snapshot.model_dump_json()
        assert "first-token" not in serialized_snapshot
        assert "second-token" not in serialized_snapshot
        assert "nested-token" not in serialized_snapshot
        preview = compile_preflight_preview(
            source=_source(workflow),
            config=config,
            runtime_snapshot=snapshot.snapshot,
            options=PreflightCompileOptions(
                project_root=tmp_path,
                orchestrator_dir=tmp_path / ".orchestrator",
                fingerprint_key_policy="persist_if_needed",
            ),
        )
        assert not preview.diagnostics
        assert preview.workflow_signature is not None
        serialized_preview = preview.model_dump_json()
        assert "first-token" not in serialized_preview
        assert "second-token" not in serialized_preview
        assert "nested-token" not in serialized_preview

        plan = PreflightExecutionPlan.from_preview(
            preview=preview,
            run_id="run",
            run_key_name="demo-run",
            context_root="/tmp/demo-run",
            manifest_root="/tmp/demo-run/manifests",
            created_at=datetime(2026, 6, 3),
        )
        serialized_plan = plan.model_dump_json()
        assert "first-token" not in serialized_plan
        assert "second-token" not in serialized_plan
        assert "nested-token" not in serialized_plan
        redacted_option = plan.runtime_config_snapshot["invoker"]["options"][
            "api_token"
        ]
        assert redacted_option["redacted"] is True
        assert len(redacted_option["fingerprint"]) == 64
        assert {
            "path": "integrations.invoker.options.api_token",
            "fingerprint": redacted_option["fingerprint"],
        } in plan.runtime_config_snapshot["config_fingerprints"]
        return preview.workflow_signature

    assert compile_with_token("first-token") != compile_with_token("second-token")
    compile_with_token({"redacted": True, "value": "nested-token"})


def test_param_tokens_cannot_survive_to_preflight_plan(tmp_path: Path) -> None:
    config = _mock_config()
    workflow = WorkflowPlan(
        name="demo",
        nodes=[
            WorkflowNode(
                id="build",
                mode="sequential",
                providers=[ProviderSpec(provider="mock")],
                prompt_segments=[
                    PromptSegment(role="shared", content="{{param:name}}")
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
    assert [
        (diagnostic.code, diagnostic.phase) for diagnostic in preview.diagnostics
    ] == [("PREFLIGHT-VALIDATION", "reference")]
    assert "composition-only template" in preview.diagnostics[0].message
    assert preview.render_plans == []
    assert preview.token_catalog == []
    assert preview.workflow_signature is None


def test_sensitive_env_and_var_fingerprints_are_persisted_and_redacted(
    tmp_path: Path,
) -> None:
    config = _mock_config()
    workflow = WorkflowPlan(
        name="demo",
        nodes=[
            WorkflowNode(
                id="build",
                mode="sequential",
                providers=[ProviderSpec(provider="mock")],
                prompt_segments=[
                    PromptSegment(
                        role="executor",
                        content="{{env:API_TOKEN}} {{var:private_key}}",
                    )
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

    def compile_once() -> str:
        preview = compile_preflight_preview(
            source=_source(workflow),
            config=config,
            runtime_snapshot=snapshot.snapshot,
            options=PreflightCompileOptions(
                project_root=tmp_path,
                orchestrator_dir=tmp_path / ".orchestrator",
                environment={"API_TOKEN": "super-secret"},
                runtime_variables={"private_key": "var-secret"},
                fingerprint_key_policy="persist_if_needed",
            ),
        )
        assert not preview.diagnostics
        assert preview.workflow_signature is not None
        serialized = preview.model_dump_json()
        assert "super-secret" not in serialized
        assert "var-secret" not in serialized
        assert preview.secret_context.get("env:API_TOKEN") == "super-secret"
        assert preview.secret_context.get("var:private_key") == "var-secret"
        assert preview.value_fingerprints == [
            {
                "fingerprint": preview.value_fingerprints[0]["fingerprint"],
                "fingerprint_schema_version": "1",
                "key": "API_TOKEN",
                "kind": "env",
                "sensitive": "true",
            },
            {
                "fingerprint": preview.value_fingerprints[1]["fingerprint"],
                "fingerprint_schema_version": "1",
                "key": "private_key",
                "kind": "var",
                "sensitive": "true",
            },
        ]
        assert all("value" not in record for record in preview.value_fingerprints)
        assert all(
            len(record["fingerprint"]) == 64 for record in preview.value_fingerprints
        )
        fingerprints_by_key = {
            record["key"]: record["fingerprint"]
            for record in preview.value_fingerprints
        }
        sensitive_tokens = {
            entry.token_kind: entry
            for entry in preview.token_catalog
            if entry.token_kind in {"env", "var"}
        }
        assert sensitive_tokens["env"].resolved == {
            "fingerprint": fingerprints_by_key["API_TOKEN"],
            "key": "API_TOKEN",
            "kind": "static_env",
            "value_handle": "env:API_TOKEN",
        }
        assert sensitive_tokens["var"].resolved == {
            "fingerprint": fingerprints_by_key["private_key"],
            "key": "private_key",
            "kind": "static_var",
            "value_handle": "var:private_key",
        }
        stream_fragments = preview.render_plans[0].streams[0].fragments
        env_fragment = next(
            fragment for fragment in stream_fragments if fragment.kind == "static_env"
        )
        var_fragment = next(
            fragment for fragment in stream_fragments if fragment.kind == "static_var"
        )
        assert env_fragment.fingerprint == fingerprints_by_key["API_TOKEN"]
        assert var_fragment.fingerprint == fingerprints_by_key["private_key"]

        plan = PreflightExecutionPlan.from_preview(
            preview=preview,
            run_id="run",
            run_key_name="demo-run",
            context_root="/tmp/demo-run",
            manifest_root="/tmp/demo-run/manifests",
            created_at=datetime(2026, 6, 3),
        )
        serialized_plan = plan.model_dump_json()
        assert "super-secret" not in serialized_plan
        assert "var-secret" not in serialized_plan
        assert plan.value_fingerprints == preview.value_fingerprints
        return preview.workflow_signature

    assert compile_once() == compile_once()
    key_path = tmp_path / ".orchestrator" / "preflight" / "fingerprint.key"
    assert key_path.exists()
    assert key_path.stat().st_size == 32


def test_absent_read_only_fingerprint_key_is_process_local_and_artifact_free(
    tmp_path: Path,
) -> None:
    config = _mock_config()
    workflow = WorkflowPlan(
        name="demo",
        nodes=[
            WorkflowNode(
                id="build",
                mode="sequential",
                providers=[ProviderSpec(provider="mock")],
                prompt_segments=[
                    PromptSegment(role="executor", content="{{env:API_TOKEN}}")
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

    def compile_once() -> str:
        preview = compile_preflight_preview(
            source=_source(workflow),
            config=config,
            runtime_snapshot=snapshot.snapshot,
            options=PreflightCompileOptions(
                project_root=tmp_path,
                orchestrator_dir=tmp_path / ".orchestrator",
                environment={"API_TOKEN": "super-secret"},
                fingerprint_key_policy="read_only",
            ),
        )
        assert not preview.diagnostics
        assert preview.workflow_signature is not None
        assert preview.fingerprint_metadata["fingerprint_key_persisted"] is False
        assert preview.fingerprint_metadata["persisted_key_path"] is None
        return preview.workflow_signature

    assert compile_once() == compile_once()
    assert not (tmp_path / ".orchestrator" / "preflight" / "fingerprint.key").exists()


def test_concurrent_first_fingerprint_key_publish_converges(
    tmp_path: Path,
    monkeypatch,
) -> None:
    worker_count = 4
    start_barrier = threading.Barrier(worker_count)
    publish_barrier = threading.Barrier(worker_count)
    original_token_bytes = preflight_secrets.secrets.token_bytes

    def synchronized_token_bytes(size: int) -> bytes:
        publish_barrier.wait(timeout=5)
        return original_token_bytes(size)

    monkeypatch.setattr(
        preflight_secrets.secrets,
        "token_bytes",
        synchronized_token_bytes,
    )

    def load_key() -> bytes:
        start_barrier.wait(timeout=5)
        result = FingerprintKeyProvider(tmp_path / ".orchestrator").load_key(
            "persist_if_needed"
        )
        assert not result.diagnostics
        assert result.persisted
        return result.key

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(load_key) for _ in range(worker_count)]
        keys = [future.result() for future in futures]

    key_path = tmp_path / ".orchestrator" / "preflight" / "fingerprint.key"
    assert key_path.exists()
    assert key_path.stat().st_size == 32
    assert set(keys) == {key_path.read_bytes()}
    assert list(key_path.parent.glob(f".{key_path.name}.*.tmp")) == []
