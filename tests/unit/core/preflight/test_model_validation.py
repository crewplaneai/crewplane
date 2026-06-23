from __future__ import annotations

import pytest
from pydantic import ValidationError

from crewplane.core.preflight.models import (
    ArtifactContract,
    Fragment,
    PreflightCompilationPreview,
    PreflightDiagnostic,
    PreflightExecutionNode,
    ProviderRecord,
)
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole


def _artifact_contract() -> ArtifactContract:
    return ArtifactContract(output_path="build-result.md")


def _provider_record() -> ProviderRecord:
    return ProviderRecord(
        provider="mock",
        role=ProviderRole.EXECUTOR,
        task_id="mock_executor_0",
        agent_config_key="mock",
        invoker_alias="mock",
        agent_config_signature="agent-signature",
        invoker_config_signature="invoker-signature",
    )


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (
            {
                "fragment_index": 0,
                "kind": "literal",
                "source_role": PromptSegmentRole.SHARED,
                "text": "hello",
                "content_ref": "content/file.txt",
            },
            "literal fragments must not define payload field",
        ),
        (
            {
                "fragment_index": 0,
                "kind": "static_file_content",
                "source_role": PromptSegmentRole.SHARED,
                "content_ref": "content/file.txt",
                "text": "hello",
            },
            "static_file_content fragments must not define payload field",
        ),
        (
            {
                "fragment_index": 0,
                "kind": "workspace_file_locator",
                "source_role": PromptSegmentRole.SHARED,
                "locator": {"locator_id": "workspace:0"},
                "content_ref": "content/file.txt",
            },
            "workspace_file_locator fragments must not define payload field",
        ),
        (
            {
                "fragment_index": 0,
                "kind": "runtime_locator_lookup",
                "source_role": PromptSegmentRole.SHARED,
                "locator": {"node_id": "build", "artifact_name": "output"},
                "content_ref": "content/file.txt",
            },
            "runtime_locator_lookup fragments must not define payload field",
        ),
        (
            {
                "fragment_index": 0,
                "kind": "static_env",
                "source_role": PromptSegmentRole.SHARED,
                "key": "API_TOKEN",
                "value_handle": "env:API_TOKEN",
                "fingerprint": "abc",
                "locator": {"node_id": "build", "artifact_name": "output"},
            },
            "static_env fragments must not define payload field",
        ),
    ],
)
def test_fragment_rejects_kind_unrelated_payload_fields(
    payload: dict[str, object],
    match: str,
) -> None:
    with pytest.raises(ValidationError, match=match):
        Fragment.model_validate(payload)


def test_static_value_fragment_requires_exactly_one_value_source() -> None:
    payload = {
        "fragment_index": 0,
        "kind": "static_var",
        "source_role": PromptSegmentRole.SHARED,
        "key": "name",
        "value_handle": "var:name",
        "value_stored": "stored",
        "fingerprint": "abc",
    }

    with pytest.raises(ValidationError, match="exactly one handle or stored value"):
        Fragment.model_validate(payload)


def test_workspace_file_locator_fragment_requires_locator_id() -> None:
    payload = {
        "fragment_index": 0,
        "kind": "workspace_file_locator",
        "source_role": PromptSegmentRole.SHARED,
        "locator": {"workspace_relative_path": "docs/plan.md"},
    }

    with pytest.raises(ValidationError, match="locator_id"):
        Fragment.model_validate(payload)


def test_runtime_locator_fragment_requires_locator_metadata() -> None:
    payload = {
        "fragment_index": 0,
        "kind": "runtime_locator_lookup",
        "source_role": PromptSegmentRole.SHARED,
        "locator": {"node_id": "build"},
    }

    with pytest.raises(ValidationError, match="node_id and artifact_name"):
        Fragment.model_validate(payload)


def test_input_preflight_node_rejects_provider_execution_fields() -> None:
    with pytest.raises(ValidationError, match="render_plan_id"):
        PreflightExecutionNode(
            id="input",
            mode="input",
            render_plan_id="input",
            artifact_contract=_artifact_contract(),
        )

    with pytest.raises(ValidationError, match="provider_records"):
        PreflightExecutionNode(
            id="input",
            mode="input",
            provider_records=[_provider_record()],
            artifact_contract=_artifact_contract(),
        )


def test_provider_preflight_node_rejects_input_source_fields() -> None:
    with pytest.raises(ValidationError, match="input_content_ref"):
        PreflightExecutionNode(
            id="build",
            mode="sequential",
            artifact_contract=_artifact_contract(),
            input_content_ref="static/content.txt",
        )

    with pytest.raises(ValidationError, match="input_workspace_file_locator_id"):
        PreflightExecutionNode(
            id="build",
            mode="parallel",
            artifact_contract=_artifact_contract(),
            input_workspace_file_locator_id="workspace:0",
        )


def test_failed_preview_can_carry_partial_provider_node_for_diagnostics() -> None:
    node = PreflightExecutionNode(
        id="build",
        mode="sequential",
        artifact_contract=_artifact_contract(),
    )

    preview = PreflightCompilationPreview(
        workflow_name="demo",
        execution_order=["build"],
        nodes=[node],
        diagnostics=[
            PreflightDiagnostic(
                code="TEMPLATE-PLAN",
                phase="template_plan",
                message="template failed before render plan completion",
            )
        ],
    )

    assert preview.has_errors()
