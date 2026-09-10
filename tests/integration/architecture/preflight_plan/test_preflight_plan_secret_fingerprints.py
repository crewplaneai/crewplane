from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console

from crewplane.architecture.contracts import (
    CanonicalIntegrationConfig,
)
from crewplane.bootstrap import build_runtime_config_snapshot
from crewplane.core.config import (
    Config,
    IntegrationSpec,
)
from crewplane.core.preflight import (
    PreflightCompileOptions,
    PreflightExecutionPlan,
    compile_preflight_preview,
    signature_for_payload,
)
from crewplane.core.prompt_segments import PromptSegment, PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import (
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from tests.integration.architecture.preflight_plan.preflight_plan_secret_fingerprints_support import (
    FIXED_FINGERPRINT_KEY,
)

from .helpers import literal_workflow, make_source, mock_config


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


def test_sensitive_config_values_are_hmac_fingerprinted_and_redacted(
    tmp_path: Path,
) -> None:
    workflow = literal_workflow()

    def compile_with_secret(secret_value: str, split_arg: bool = False) -> str:
        config = mock_config()
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
            console=Console(file=None),
            no_live=True,
        )
        assert secret_value not in snapshot.snapshot.model_dump_json()
        assert "--api-key=" not in snapshot.snapshot.model_dump_json()
        preview = compile_preflight_preview(
            source=make_source(workflow),
            config=config,
            runtime_snapshot=snapshot.snapshot,
            options=PreflightCompileOptions(
                project_root=tmp_path,
                state_dir=tmp_path / ".crewplane",
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
            project_root=tmp_path.as_posix(),
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
    key_path = tmp_path / ".crewplane" / "preflight" / "fingerprint.key"
    assert key_path.exists()
    assert key_path.stat().st_size == 32


def test_sensitive_adapter_options_are_hmac_fingerprinted_and_redacted(
    tmp_path: Path,
) -> None:
    workflow = literal_workflow()

    def compile_with_token(token_value: object) -> str:
        config = mock_config()
        assert config.settings is not None
        config.settings.integrations.invoker = IntegrationSpec(
            implementation=f"{__name__}:SensitiveOptionInvokerAdapter",
            options={"api_token": token_value},
        )
        snapshot = build_runtime_config_snapshot(
            config=config,
            console=Console(file=None),
            no_live=True,
        )
        serialized_snapshot = snapshot.snapshot.model_dump_json()
        assert "first-token" not in serialized_snapshot
        assert "second-token" not in serialized_snapshot
        assert "nested-token" not in serialized_snapshot
        preview = compile_preflight_preview(
            source=make_source(workflow),
            config=config,
            runtime_snapshot=snapshot.snapshot,
            options=PreflightCompileOptions(
                project_root=tmp_path,
                state_dir=tmp_path / ".crewplane",
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
            project_root=tmp_path.as_posix(),
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
        handle = redacted_option["value_handle"]
        assert handle == "config:/integrations/invoker/options/api_token"
        assert isinstance(handle, str)
        captured_value = preview.secret_context.get_config_value(handle)
        assert type(captured_value) is type(token_value)
        assert captured_value == token_value
        assert {
            "path": "/integrations/invoker/options/api_token",
            "fingerprint": redacted_option["fingerprint"],
        } in plan.runtime_config_snapshot["config_fingerprints"]
        return preview.workflow_signature

    assert compile_with_token("first-token") != compile_with_token("second-token")
    compile_with_token(None)
    compile_with_token({"redacted": True, "value": "nested-token"})


def test_sensitive_adapter_options_discard_raw_redaction_metadata() -> None:
    raw_fingerprint = "a" * 64
    raw_handle = "config:/attacker-controlled-path"
    config = mock_config()
    assert config.settings is not None
    config.settings.integrations.invoker = IntegrationSpec(
        implementation=f"{__name__}:SensitiveOptionInvokerAdapter",
        options={
            "api_token": {
                "redacted": True,
                "fingerprint": raw_fingerprint,
                "value_handle": raw_handle,
            }
        },
    )

    snapshot = build_runtime_config_snapshot(
        config=config,
        console=Console(file=None),
        no_live=True,
    ).snapshot
    fingerprinted = snapshot.with_sensitive_config_fingerprints(FIXED_FINGERPRINT_KEY)
    redacted_option = fingerprinted.invoker.options["api_token"]

    assert redacted_option["redacted"] is True
    assert redacted_option["fingerprint"] != raw_fingerprint
    assert redacted_option["value_handle"] != raw_handle
    assert raw_fingerprint not in fingerprinted.model_dump_json()
    assert raw_handle not in fingerprinted.model_dump_json()


def test_param_tokens_cannot_survive_to_preflight_plan(tmp_path: Path) -> None:
    config = mock_config()
    workflow = WorkflowPlan(
        name="demo",
        nodes=[
            WorkflowNode(
                id="build",
                mode="sequential",
                providers=[ProviderSpec(provider="mock")],
                prompt_segments=[
                    PromptSegment(
                        role=PromptSegmentRole.SHARED, content="{{param:name}}"
                    )
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
        source=make_source(workflow),
        config=config,
        runtime_snapshot=snapshot.snapshot,
        options=PreflightCompileOptions(
            project_root=tmp_path,
            state_dir=tmp_path / ".crewplane",
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
    config = mock_config()
    workflow = WorkflowPlan(
        name="demo",
        nodes=[
            WorkflowNode(
                id="build",
                mode="sequential",
                providers=[ProviderSpec(provider="mock")],
                prompt_segments=[
                    PromptSegment(
                        role=ProviderRole.EXECUTOR,
                        content="{{env:API_TOKEN}} {{var:private_key}}",
                    )
                ],
            )
        ],
    )
    snapshot = build_runtime_config_snapshot(
        config=config,
        console=Console(file=None),
        no_live=True,
    )

    def compile_once() -> str:
        preview = compile_preflight_preview(
            source=make_source(workflow),
            config=config,
            runtime_snapshot=snapshot.snapshot,
            options=PreflightCompileOptions(
                project_root=tmp_path,
                state_dir=tmp_path / ".crewplane",
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
                "fingerprint_payload_version": "1",
                "key": "API_TOKEN",
                "kind": "env",
                "sensitive": "true",
            },
            {
                "fingerprint": preview.value_fingerprints[1]["fingerprint"],
                "fingerprint_payload_version": "1",
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
            project_root=tmp_path.as_posix(),
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
    key_path = tmp_path / ".crewplane" / "preflight" / "fingerprint.key"
    assert key_path.exists()
    assert key_path.stat().st_size == 32
