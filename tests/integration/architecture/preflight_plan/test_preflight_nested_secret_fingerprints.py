from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from crewplane.architecture.contracts import (
    CanonicalIntegrationConfig,
    SignatureScope,
)
from crewplane.core.config import (
    Config,
)
from crewplane.core.preflight import (
    PreflightCompilationPreview,
    PreflightCompileOptions,
    PreflightExecutionPlan,
    compile_preflight_preview,
)
from crewplane.core.preflight.runtime_config import (
    RuntimeConfigSnapshot,
    RuntimeConfigSnapshotOptions,
)
from crewplane.core.prompt_segments import PromptSegment, PromptSegmentRole
from crewplane.core.workflow.models import (
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from tests.integration.architecture.preflight_plan.preflight_plan_secret_fingerprints_support import (
    FIXED_FINGERPRINT_KEY,
)

from .helpers import literal_workflow, make_source, mock_config

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
    key_path.write_bytes(FIXED_FINGERPRINT_KEY)
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
