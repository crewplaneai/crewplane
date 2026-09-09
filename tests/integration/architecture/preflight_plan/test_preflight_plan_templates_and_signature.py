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

from crewplane.architecture.contracts import CanonicalIntegrationConfig
from crewplane.bootstrap import build_runtime_config_snapshot
from crewplane.core.config import (
    Config,
)
from crewplane.core.preflight import (
    PreflightCompilationPreview,
    PreflightCompileOptions,
    PreflightExecutionPlan,
    compile_preflight_preview,
    load_workflow_source_for_preflight,
)
from crewplane.core.preflight.models import WorkspaceSelectionRecord
from crewplane.core.prompt_segments import PromptSegment, PromptSegmentRole
from crewplane.core.workflow.models import (
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.version import SCHEMA_VERSION
from tests.helpers.resume import make_plan, make_snapshot_workspace_plan

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


def test_binary_static_file_token_fails_deterministically(tmp_path: Path) -> None:
    binary_file = tmp_path / "payload.bin"
    binary_file.write_bytes(b"\xff\xfe\x00")
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
                        role=PromptSegmentRole.SHARED, content="{{file:payload.bin}}"
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
    config = mock_config()
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
            state_dir=root / ".crewplane",
            fingerprint_key_policy="read_only",
        ),
    )

    assert not preview.diagnostics
    assert set(preview.static_file_payloads.values()) == {b"project context"}
    assert all(
        resource.source_root == root.as_posix() for resource in preview.static_resources
    )


def test_persisted_plan_keeps_preview_workflow_signature(tmp_path: Path) -> None:
    config = mock_config()
    workflow = literal_workflow()
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
    config = mock_config()
    workflow = literal_workflow()
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
            project_root=root,
            state_dir=root / ".crewplane",
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


def test_preflight_execution_plan_rejects_provider_node_without_render_plan(
    tmp_path: Path,
) -> None:
    payload = _persisted_plan_payload(tmp_path)
    payload["nodes"][0]["render_plan_id"] = None

    with pytest.raises(ValidationError, match="must define render_plan_id"):
        PreflightExecutionPlan(**payload)


def test_preflight_execution_plan_rejects_provider_node_without_provider_records(
    tmp_path: Path,
) -> None:
    payload = _persisted_plan_payload(tmp_path)
    payload["nodes"][0]["provider_records"] = []

    with pytest.raises(ValidationError, match="at least one provider record"):
        PreflightExecutionPlan(**payload)


def test_preflight_execution_plan_rejects_present_disabled_workspace_policy(
    tmp_path: Path,
) -> None:
    payload = _persisted_plan_payload(tmp_path)
    policy = WorkspaceSelectionRecord().model_dump(mode="python")
    policy.update(
        {
            "logical_worktree_name": "primary",
            "declaration_kind": "worktree",
            "writable": True,
            "lineage_producer": True,
        }
    )
    policy["branch_export"]["create_branch"] = True
    payload["nodes"][0]["workspace_policy"] = policy

    with pytest.raises(
        ValidationError,
        match="must omit a disabled workspace_policy",
    ):
        PreflightExecutionPlan(**payload)


def test_preflight_execution_plan_rejects_input_node_without_input_source(
    tmp_path: Path,
) -> None:
    payload = _persisted_plan_payload(tmp_path)
    node = payload["nodes"][0]
    node["mode"] = "input"
    node["render_plan_id"] = None
    node["provider_records"] = []
    node["input_content_ref"] = None
    node["input_workspace_file_locator_id"] = None

    with pytest.raises(ValidationError, match="exactly one input source reference"):
        PreflightExecutionPlan(**payload)


def test_preflight_execution_plan_rejects_input_node_with_ambiguous_input_sources(
    tmp_path: Path,
) -> None:
    payload = _persisted_plan_payload(tmp_path)
    node = payload["nodes"][0]
    node["mode"] = "input"
    node["render_plan_id"] = None
    node["provider_records"] = []
    node["input_content_ref"] = "static/input.txt"
    node["input_workspace_file_locator_id"] = "workspace:input"

    with pytest.raises(ValidationError, match="exactly one input source reference"):
        PreflightExecutionPlan(**payload)


def test_preflight_execution_plan_rejects_input_node_with_blank_input_source_reference(
    tmp_path: Path,
) -> None:
    payload = _persisted_plan_payload(tmp_path)
    node = payload["nodes"][0]
    node["mode"] = "input"
    node["render_plan_id"] = None
    node["provider_records"] = []
    node["input_content_ref"] = " "
    node["input_workspace_file_locator_id"] = None

    with pytest.raises(ValidationError, match="exactly one input source reference"):
        PreflightExecutionPlan(**payload)


def test_preflight_execution_plan_rejects_provider_node_with_blank_render_plan_id(
    tmp_path: Path,
) -> None:
    payload = _persisted_plan_payload(tmp_path)
    payload["nodes"][0]["render_plan_id"] = " "

    with pytest.raises(ValidationError, match="must define render_plan_id"):
        PreflightExecutionPlan(**payload)


def test_preflight_execution_plan_rejects_dependency_graph_drift(
    tmp_path: Path,
) -> None:
    payload = _persisted_plan_payload(tmp_path)
    payload["nodes"][0]["dependencies"] = ["build"]

    with pytest.raises(ValidationError, match="disagree with the compiled"):
        PreflightExecutionPlan(**payload)


def test_preflight_execution_plan_rejects_unknown_dependency_graph_nodes(
    tmp_path: Path,
) -> None:
    payload = _persisted_plan_payload(tmp_path)
    payload["dependency_graph"] = [
        {
            "source_node": "unknown",
            "target_node": "build",
            "artifact_name": "output",
            "dependency_signature": "unknown-edge",
            "target_locator": "unknown.output",
            "artifact_key": "output",
        }
    ]

    with pytest.raises(ValidationError, match="unknown source node"):
        PreflightExecutionPlan(**payload)


@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    [
        ("findings", True, "cannot define findings"),
        ("dependencies", ["build"], "cannot define findings or dependencies"),
        (
            "execution_policy",
            {"continue_on_failure": True},
            "contains provider execution policy",
        ),
    ],
)
def test_preflight_execution_plan_rejects_input_provider_authority(
    tmp_path: Path,
    field_name: str,
    value: object,
    message: str,
) -> None:
    payload = _persisted_plan_payload(tmp_path)
    node = payload["nodes"][0]
    node["mode"] = "input"
    node["render_plan_id"] = None
    node["provider_records"] = []
    node["input_content_ref"] = "static/input.txt"
    node["input_workspace_file_locator_id"] = None
    node[field_name] = value

    with pytest.raises(ValidationError, match=message):
        PreflightExecutionPlan(**payload)


def test_preflight_input_node_accepts_global_concurrency_policy(
    tmp_path: Path,
) -> None:
    payload = _persisted_plan_payload(tmp_path)
    node = payload["nodes"][0]
    node["mode"] = "input"
    node["render_plan_id"] = None
    node["provider_records"] = []
    node["input_content_ref"] = "static/input.txt"
    node["input_workspace_file_locator_id"] = None
    node["execution_policy"]["token_budget"] = None
    payload["render_plans"] = []
    payload["runtime_config_snapshot"]["execution"].update(
        {
            "max_concurrent_nodes": 2,
            "max_parallel_invocations": 3,
        }
    )
    node["execution_policy"]["concurrency_policy"] = {
        "max_concurrent_nodes": 2,
        "max_parallel_invocations": 3,
    }

    PreflightExecutionPlan(**payload)


def test_preflight_execution_plan_rejects_single_provider_reviewer(
    tmp_path: Path,
) -> None:
    payload = _persisted_plan_payload(tmp_path)
    payload["nodes"][0]["provider_records"][0]["role"] = "reviewer"

    with pytest.raises(ValidationError, match="single-provider node"):
        PreflightExecutionPlan(**payload)


def test_preflight_execution_plan_rejects_malformed_fragment_payload(
    tmp_path: Path,
) -> None:
    payload = _persisted_plan_payload(tmp_path)
    fragment = payload["render_plans"][0]["streams"][0]["fragments"][0]
    fragment["content_ref"] = "static/unrelated.txt"

    with pytest.raises(ValidationError, match="literal fragments must not define"):
        PreflightExecutionPlan(**payload)


def test_preflight_execution_plan_rejects_persisted_param_token(
    tmp_path: Path,
) -> None:
    payload = _persisted_plan_payload(tmp_path)
    payload["token_catalog"] = [
        {
            "occurrence_id": "build:executor:0:0",
            "node_id": "build",
            "target_role": "executor",
            "source_role": "shared",
            "raw_token": "{{param:name}}",
            "token_kind": "param",
            "fragment_index": 0,
            "signature": "param-signature",
        }
    ]

    with pytest.raises(
        ValidationError,
        match="Param tokens are composition-only",
    ):
        PreflightExecutionPlan(**payload)


@pytest.mark.parametrize("schema_version", [None, "0.9", "99.0"])
def test_serialized_plan_requires_exact_schema_identity(
    tmp_path: Path,
    schema_version: str | None,
) -> None:
    payload = _persisted_plan_payload(tmp_path)
    if schema_version is None:
        payload.pop("plan_schema_version")
    else:
        payload["plan_schema_version"] = schema_version

    with pytest.raises(ValidationError):
        PreflightExecutionPlan.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "token_budget",
    [
        {"fail_threshold_chars": 0, "warn_threshold_chars": None},
        {"fail_threshold_chars": None, "warn_threshold_chars": -1},
        {"fail_threshold_chars": 50, "warn_threshold_chars": 100},
    ],
)
def test_serialized_plan_rejects_invalid_token_budgets(
    tmp_path: Path,
    token_budget: dict[str, int | None],
) -> None:
    payload = _persisted_plan_payload(tmp_path)
    payload["nodes"][0]["execution_policy"]["token_budget"] = token_budget

    with pytest.raises(ValidationError):
        PreflightExecutionPlan.model_validate_json(json.dumps(payload))


def test_serialized_plan_rejects_invalid_consensus_value(tmp_path: Path) -> None:
    payload = _persisted_review_plan_payload(tmp_path)
    payload["nodes"][0]["execution_policy"]["consensus_on_exhaustion"] = (
        "unknown-policy"
    )

    with pytest.raises(ValidationError):
        PreflightExecutionPlan.model_validate_json(json.dumps(payload))


def test_serialized_plan_rejects_conflicting_consensus_authorities(
    tmp_path: Path,
) -> None:
    payload = _persisted_review_plan_payload(tmp_path)
    payload["nodes"][0]["execution_policy"]["consensus_on_exhaustion"] = "fatal"
    payload["runtime_config_snapshot"]["execution"][
        "sequential_consensus_on_exhaustion"
    ] = "continue"

    with pytest.raises(ValidationError, match="conflicts with the signed"):
        PreflightExecutionPlan.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "mutation",
    [
        "negative_size",
        "missing_static_metadata",
        "dynamic_with_static_metadata",
        "unknown_node",
        "wrong_target",
        "duplicate_locator",
    ],
)
def test_serialized_plan_rejects_impossible_workspace_locator_states(
    mutation: str,
) -> None:
    payload = json.loads(make_snapshot_workspace_plan().model_dump_json())
    locator = payload["workspace_file_locators"][0]
    if mutation == "negative_size":
        locator["byte_size"] = -1
    elif mutation == "missing_static_metadata":
        locator["content_ref"] = None
    elif mutation == "dynamic_with_static_metadata":
        locator["source_class"] = "runtime_dynamic"
    elif mutation == "unknown_node":
        locator["node_id"] = "unknown"
    elif mutation == "wrong_target":
        locator["target"] = "executor_prompt"
    else:
        duplicate = dict(locator)
        duplicate["occurrence_id"] = "duplicate-occurrence"
        payload["workspace_file_locators"].append(duplicate)

    with pytest.raises(ValidationError):
        PreflightExecutionPlan.model_validate_json(json.dumps(payload))


def test_serialized_plan_requires_exact_dependency_for_runtime_locator() -> None:
    payload = json.loads(make_plan().model_dump_json())
    payload["render_plans"][1]["streams"] = [
        {
            "target_role": "executor",
            "fragments": [
                {
                    "fragment_index": 0,
                    "kind": "runtime_locator_lookup",
                    "source_role": "shared",
                    "locator": {
                        "node_id": "a",
                        "artifact_name": "output_size",
                    },
                }
            ],
        }
    ]

    with pytest.raises(ValidationError, match="matching dependency edge"):
        PreflightExecutionPlan.model_validate_json(json.dumps(payload))


def _persisted_review_plan_payload(root: Path) -> dict[str, Any]:
    payload = _persisted_plan_payload(root)
    executor = payload["nodes"][0]["provider_records"][0]
    reviewer = dict(executor)
    reviewer.update(
        {
            "provider": "reviewer",
            "role": "reviewer",
            "task_id": "reviewer_reviewer_0",
            "agent_config_key": "mock",
        }
    )
    payload["nodes"][0]["provider_records"].append(reviewer)
    payload["nodes"][0]["execution_policy"]["consensus_on_exhaustion"] = "continue"
    payload["runtime_config_snapshot"]["execution"][
        "sequential_consensus_on_exhaustion"
    ] = "continue"
    return payload
