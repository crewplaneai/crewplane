from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

import crewplane.core.preflight.secrets as preflight_secrets
from crewplane.architecture.contracts import (
    CanonicalIntegrationConfig,
    SignatureScope,
)
from crewplane.bootstrap import build_runtime_config_snapshot
from crewplane.core.config import (
    Config,
    IntegrationSpec,
)
from crewplane.core.preflight import (
    FingerprintKeyCache,
    FingerprintKeyProvider,
    PreflightCompilationPreview,
    PreflightCompileOptions,
    PreflightExecutionPlan,
    compile_preflight_preview,
    signature_for_payload,
)
from crewplane.core.preflight.runtime_config import (
    RuntimeConfigSnapshot,
    RuntimeConfigSnapshotOptions,
)
from crewplane.core.prompt_segments import PromptSegment, PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import (
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
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


def _compile_signature(root: Path, no_live: bool) -> str:
    config = mock_config()
    workflow = literal_workflow()
    snapshot = build_runtime_config_snapshot(
        config=config,
        console=Console(file=None),
        no_live=no_live,
    )
    preview = compile_preflight_preview(
        source=make_source(workflow),
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
    fingerprinted = snapshot.with_sensitive_config_fingerprints(_FIXED_FINGERPRINT_KEY)
    redacted_option = fingerprinted.invoker.options["api_token"]

    assert redacted_option["redacted"] is True
    assert redacted_option["fingerprint"] != raw_fingerprint
    assert redacted_option["value_handle"] != raw_handle
    assert raw_fingerprint not in fingerprinted.model_dump_json()
    assert raw_handle not in fingerprinted.model_dump_json()


_FIXED_FINGERPRINT_KEY = b"nested-adapter-fingerprint-key!!"
_EXPECTED_NESTED_ADAPTER_FINGERPRINTS = {
    "/integrations/invoker/options/routes/0/api_token": (
        "0425f1b8e3237be2d55eb66558b6d7b492e0608a3eff6adf0f8216991467a134"
    ),
    "/integrations/invoker/options/routes/1/auth~1config/value~0raw": (
        "dfd3ac0f38e5b2e307543b0aed8311308ccf96cc16acf3be2dea472d72f2f72d"
    ),
    "/integrations/artifacts/options/routes/0/api_token": (
        "811f58b07e24c2c3798d367b64fea5e7a4c1b3ab94df0ad9b6fadbf8bbaaba99"
    ),
    "/integrations/artifacts/options/routes/1/auth~1config/value~0raw": (
        "c9c16ff75b8575855d159492e24dc9e1696667dca171e1fd90123e24c51c7c2d"
    ),
    "/integrations/ui/options/routes/0/api_token": (
        "38517339bb4538835d8922faa4f427c73f0e75c3d641eb2e9d02aa59c7ea4d50"
    ),
    "/integrations/ui/options/routes/1/auth~1config/value~0raw": (
        "f69d3938772b7eeb7bfb92506b53a2409905aadb984203a286499a409c56416e"
    ),
}


def _nested_secret_integration(
    integration_name: str,
    scope: SignatureScope,
    secret_version: str,
) -> CanonicalIntegrationConfig:
    explicit_pointer = "/routes/1/auth~1config/value~0raw"
    return CanonicalIntegrationConfig(
        implementation=f"custom-{integration_name}",
        resolved_identity=f"tests:{integration_name}",
        options={
            "routes": [
                {"api_token": f"{integration_name}-pattern-{secret_version}"},
                {
                    "auth/config": {
                        "value~raw": (f"{integration_name}-explicit-{secret_version}")
                    }
                },
            ],
        },
        sensitive_options=[explicit_pointer],
        option_scopes={"routes": scope},
    )


def _runtime_snapshot_with_nested_adapter_secrets(
    config: Config,
    secret_version: str,
) -> RuntimeConfigSnapshot:
    return RuntimeConfigSnapshot.build(
        config=config,
        invoker=_nested_secret_integration(
            "invoker",
            "execution",
            secret_version,
        ),
        artifacts=_nested_secret_integration(
            "artifacts",
            "artifact",
            secret_version,
        ),
        ui=_nested_secret_integration("ui", "observer", secret_version),
        options=RuntimeConfigSnapshotOptions(no_live=True),
    )


def _compile_with_nested_adapter_secrets(
    root: Path,
    secret_version: str,
) -> tuple[
    RuntimeConfigSnapshot,
    PreflightCompilationPreview,
    PreflightExecutionPlan,
]:
    key_path = root / ".crewplane" / "preflight" / "fingerprint.key"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(_FIXED_FINGERPRINT_KEY)
    key_path.chmod(0o600)
    config = mock_config()
    snapshot = _runtime_snapshot_with_nested_adapter_secrets(
        config,
        secret_version,
    )
    preview = compile_preflight_preview(
        source=make_source(literal_workflow()),
        config=config,
        runtime_snapshot=snapshot,
        options=PreflightCompileOptions(
            project_root=root,
            state_dir=root / ".crewplane",
            fingerprint_key_policy="read_only",
        ),
    )
    assert not preview.diagnostics
    assert preview.workflow_signature is not None
    plan = PreflightExecutionPlan.from_preview(
        preview=preview,
        run_id="run",
        run_key_name="demo-run",
        project_root=root.as_posix(),
        context_root="/tmp/demo-run",
        manifest_root="/tmp/demo-run/manifests",
        created_at=datetime(2026, 6, 3),
    )
    return snapshot, preview, plan


def test_nested_adapter_secrets_stay_out_of_serialized_preflight_surfaces(
    tmp_path: Path,
) -> None:
    initial_snapshot, preview, plan = _compile_with_nested_adapter_secrets(
        tmp_path,
        "first",
    )
    snapshot = preview.runtime_config_snapshot
    serialized_surfaces = [
        initial_snapshot.model_dump_json(),
        json.dumps(initial_snapshot.redacted_payload(), sort_keys=True),
        repr(initial_snapshot),
        snapshot.model_dump_json(),
        json.dumps(snapshot.redacted_payload(), sort_keys=True),
        preview.model_dump_json(),
        plan.model_dump_json(),
        repr(snapshot),
        repr(preview),
        repr(preview.diagnostics),
    ]

    for integration_name in ("invoker", "artifacts", "ui"):
        pattern_secret = f"{integration_name}-pattern-first"
        explicit_secret = f"{integration_name}-explicit-first"
        assert all(pattern_secret not in surface for surface in serialized_surfaces)
        assert all(explicit_secret not in surface for surface in serialized_surfaces)

        pattern_path = f"/integrations/{integration_name}/options/routes/0/api_token"
        explicit_path = (
            f"/integrations/{integration_name}/options/routes/1/auth~1config/value~0raw"
        )
        integration = plan.runtime_config_snapshot[integration_name]
        assert integration["sensitive_options"] == [
            "/routes/0/api_token",
            "/routes/1/auth~1config/value~0raw",
        ]
        routes = integration["options"]["routes"]
        redacted_values = {
            pattern_path: routes[0]["api_token"],
            explicit_path: routes[1]["auth/config"]["value~raw"],
        }
        raw_values = {
            pattern_path: pattern_secret,
            explicit_path: explicit_secret,
        }
        expected_option_fingerprints = []
        for path, redacted_value in redacted_values.items():
            expected_fingerprint = _EXPECTED_NESTED_ADAPTER_FINGERPRINTS[path]
            assert redacted_value == {
                "fingerprint": expected_fingerprint,
                "redacted": True,
                "value_handle": f"config:{path}",
            }
            assert preview.secret_context.get(f"config:{path}") == raw_values[path]
            expected_option_fingerprints.append(
                {"path": path, "fingerprint": expected_fingerprint}
            )
        assert integration["option_fingerprints"] == expected_option_fingerprints

    expected_paths = sorted(
        f"/integrations/{integration_name}/options/routes/{suffix}"
        for integration_name in ("invoker", "artifacts", "ui")
        for suffix in ("0/api_token", "1/auth~1config/value~0raw")
    )
    assert plan.runtime_config_snapshot["sensitive_config_paths"] == expected_paths
    assert plan.runtime_config_snapshot["config_fingerprints"] == sorted(
        [
            fingerprint
            for integration_name in ("invoker", "artifacts", "ui")
            for fingerprint in plan.runtime_config_snapshot[integration_name][
                "option_fingerprints"
            ]
        ],
        key=lambda item: item["path"],
    )


def test_nested_adapter_secrets_are_redacted_on_preflight_failure(
    tmp_path: Path,
) -> None:
    config = mock_config()
    snapshot = _runtime_snapshot_with_nested_adapter_secrets(config, "failure")
    workflow = WorkflowPlan(
        name="invalid-provider",
        nodes=[
            WorkflowNode(
                id="build",
                mode="sequential",
                providers=[ProviderSpec(provider="missing")],
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="hello")
                ],
            )
        ],
    )

    preview = compile_preflight_preview(
        source=make_source(workflow),
        config=config,
        runtime_snapshot=snapshot,
        options=PreflightCompileOptions(
            project_root=tmp_path,
            state_dir=tmp_path / ".crewplane",
            fingerprint_key_policy="read_only",
        ),
    )
    serialized_diagnostics = json.dumps(
        [diagnostic.model_dump(mode="json") for diagnostic in preview.diagnostics],
        sort_keys=True,
    )

    assert preview.has_errors()
    for integration_name in ("invoker", "artifacts", "ui"):
        for secret_kind in ("pattern", "explicit"):
            raw_secret = f"{integration_name}-{secret_kind}-failure"
            assert raw_secret not in preview.model_dump_json()
            assert raw_secret not in repr(preview)
            assert raw_secret not in serialized_diagnostics


@pytest.mark.parametrize(
    ("integration_name", "signature_changes"),
    [("invoker", True), ("artifacts", True), ("ui", False)],
)
def test_nested_adapter_secret_fingerprints_change_only_effective_signatures(
    tmp_path: Path,
    integration_name: str,
    signature_changes: bool,
) -> None:
    _, first, _ = _compile_with_nested_adapter_secrets(tmp_path, "first")
    second_integrations = {
        "invoker": _nested_secret_integration("invoker", "execution", "first"),
        "artifacts": _nested_secret_integration("artifacts", "artifact", "first"),
        "ui": _nested_secret_integration("ui", "observer", "first"),
    }
    scope = {
        "invoker": "execution",
        "artifacts": "artifact",
        "ui": "observer",
    }[integration_name]
    second_integrations[integration_name] = _nested_secret_integration(
        integration_name,
        scope,
        "second",
    )
    config = mock_config()
    second_snapshot = RuntimeConfigSnapshot.build(
        config=config,
        invoker=second_integrations["invoker"],
        artifacts=second_integrations["artifacts"],
        ui=second_integrations["ui"],
        options=RuntimeConfigSnapshotOptions(no_live=True),
    )
    second = compile_preflight_preview(
        source=make_source(literal_workflow()),
        config=config,
        runtime_snapshot=second_snapshot,
        options=PreflightCompileOptions(
            project_root=tmp_path,
            state_dir=tmp_path / ".crewplane",
            fingerprint_key_policy="read_only",
        ),
    )
    assert not second.diagnostics
    assert second.workflow_signature is not None

    first_fingerprints = {
        item["path"]: item["fingerprint"]
        for item in first.runtime_config_snapshot.config_fingerprints
    }
    second_fingerprints = {
        item["path"]: item["fingerprint"]
        for item in second.runtime_config_snapshot.config_fingerprints
    }
    changed_path_prefix = f"/integrations/{integration_name}/options/"
    changed_paths = [
        path for path in first_fingerprints if path.startswith(changed_path_prefix)
    ]
    assert changed_paths
    assert all(
        first_fingerprints[path] != second_fingerprints[path] for path in changed_paths
    )
    assert (first.workflow_signature != second.workflow_signature) is signature_changes
    assert (
        first.effective_runtime_config_signature
        != second.effective_runtime_config_signature
    ) is signature_changes


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


def test_absent_read_only_fingerprint_key_is_run_scoped_and_artifact_free(
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
                        role=PromptSegmentRole.EXECUTOR, content="{{env:API_TOKEN}}"
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

    def compile_once(options: PreflightCompileOptions) -> str:
        preview = compile_preflight_preview(
            source=make_source(workflow),
            config=config,
            runtime_snapshot=snapshot.snapshot,
            options=options,
        )
        assert not preview.diagnostics
        assert preview.workflow_signature is not None
        assert preview.fingerprint_metadata["fingerprint_key_persisted"] is False
        assert preview.fingerprint_metadata["persisted_key_path"] is None
        return preview.workflow_signature

    first_run_options = PreflightCompileOptions(
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        environment={"API_TOKEN": "super-secret"},
        fingerprint_key_policy="read_only",
    )
    second_run_options = PreflightCompileOptions(
        project_root=tmp_path,
        state_dir=tmp_path / ".crewplane",
        environment={"API_TOKEN": "super-secret"},
        fingerprint_key_policy="read_only",
    )

    assert compile_once(first_run_options) == compile_once(first_run_options)
    assert compile_once(first_run_options) != compile_once(second_run_options)
    assert not (tmp_path / ".crewplane" / "preflight" / "fingerprint.key").exists()


def test_ephemeral_fingerprint_key_cache_is_explicitly_scoped(tmp_path: Path) -> None:
    state_dir = tmp_path / ".crewplane"
    cache = FingerprintKeyCache()

    first = FingerprintKeyProvider(state_dir, cache=cache).load_key("ephemeral")
    second = FingerprintKeyProvider(state_dir, cache=cache).load_key("ephemeral")
    independent = FingerprintKeyProvider(
        state_dir,
        cache=FingerprintKeyCache(),
    ).load_key("ephemeral")

    assert first.key == second.key
    assert first.key != independent.key
    assert not (state_dir / "preflight" / "fingerprint.key").exists()


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
        result = FingerprintKeyProvider(tmp_path / ".crewplane").load_key(
            "persist_if_needed"
        )
        assert not result.diagnostics
        assert result.persisted
        return result.key

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(load_key) for _ in range(worker_count)]
        keys = [future.result() for future in futures]

    key_path = tmp_path / ".crewplane" / "preflight" / "fingerprint.key"
    assert key_path.exists()
    assert key_path.stat().st_size == 32
    assert set(keys) == {key_path.read_bytes()}
    assert list(key_path.parent.glob(f".{key_path.name}.*.tmp")) == []
