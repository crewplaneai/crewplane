from __future__ import annotations

import pytest

from crewplane.architecture.contracts import (
    SUPPORTED_PROVIDER_KIND_VALUES,
    InvocationContext,
    InvocationSourceContext,
    InvocationUsage,
    InvocationWorkspaceContext,
    InvocationWorktreeContract,
    InvokerAdapterCapabilities,
    InvokerWorkspaceSupport,
    LogPresentationDescriptor,
    ProviderKind,
    ProviderTokenUsage,
    normalize_log_presentation_profile,
    validate_log_presentation_descriptor,
)
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.observability.events import (
    ExecutionEventContext,
    execution_event_log_record,
    invocation_event,
)
from crewplane.version import SCHEMA_VERSION


def test_log_presentation_profile_normalizes_safe_unknown_profiles() -> None:
    assert (
        normalize_log_presentation_profile(" Vendor.Profile-1 ") == "vendor.profile-1"
    )


def test_provider_kind_values_preserve_serialized_contract() -> None:
    assert tuple(kind.value for kind in ProviderKind) == SUPPORTED_PROVIDER_KIND_VALUES
    assert ProviderKind.CODEX.value == "codex"


def test_provider_token_usage_adds_only_complete_counters() -> None:
    existing = ProviderTokenUsage(
        input=2,
        cached_input=3,
        output=5,
        total=7,
    )
    additional = ProviderTokenUsage(
        input=11,
        cache_write=13,
        reasoning=17,
    )

    combined = existing.add_exact(additional)

    assert combined == ProviderTokenUsage(
        input=13,
        cached_input=None,
        cache_write=None,
        output=None,
        reasoning=None,
        total=None,
    )
    assert existing.output == 5
    assert additional.output is None


@pytest.mark.parametrize(
    "profile",
    ["", " ", "bad/profile", "bad profile", "x" * 65],
)
def test_log_presentation_profile_rejects_unsafe_values(profile: str) -> None:
    with pytest.raises((TypeError, ValueError)):
        normalize_log_presentation_profile(profile)


def test_log_presentation_descriptor_accepts_mapping() -> None:
    descriptor = validate_log_presentation_descriptor(
        {"format": "json_lines", "profile": "Vendor.Custom"}
    )

    assert descriptor == LogPresentationDescriptor(
        format="json_lines",
        profile="vendor.custom",
    )


def test_log_presentation_descriptor_rejects_unknown_format() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        validate_log_presentation_descriptor({"format": "yaml", "profile": "generic"})


def test_invoker_workspace_support_serializes_capability_metadata() -> None:
    support = InvokerWorkspaceSupport(
        supported=True,
        launch_mode="runtime_command_runner",
        honors_cwd=True,
        controlled_child_environment=True,
    )
    capabilities = InvokerAdapterCapabilities(workspace=support)

    assert capabilities.as_dict() == {
        "workspace": {
            "supported": True,
            "launch_mode": "runtime_command_runner",
            "honors_cwd": True,
            "controlled_child_environment": True,
        }
    }


def test_invoker_adapter_capabilities_builds_supported_workspace_metadata() -> None:
    capabilities = InvokerAdapterCapabilities.workspace_supported(
        launch_mode="mock_no_child_process",
        controlled_child_environment=False,
    )

    assert capabilities.as_dict() == {
        "workspace": {
            "supported": True,
            "launch_mode": "mock_no_child_process",
            "honors_cwd": True,
            "controlled_child_environment": False,
        }
    }


def test_invoker_workspace_support_defaults_to_unsupported() -> None:
    assert InvokerAdapterCapabilities.unsupported().as_dict() == {
        "workspace": {
            "supported": False,
            "launch_mode": None,
            "honors_cwd": False,
            "controlled_child_environment": False,
        }
    }


def test_invocation_workspace_context_records_source_identity(tmp_path) -> None:
    source = InvocationSourceContext(
        source_kind="project",
        source_node_id=None,
        source_commit="abc123",
        source_tree="def456",
        candidate_sequence=None,
    )

    workspace = InvocationWorkspaceContext(
        workspace_kind="snapshot",
        materialization="snapshot_checkout",
        logical_worktree_name="primary",
        cwd=tmp_path,
        invocation_source=source,
        worktree_contract=InvocationWorktreeContract(
            mode="blob_exact", schema_version=SCHEMA_VERSION
        ),
        candidate_commit=None,
        result_commit=None,
        writable=True,
        lineage_producer=False,
        workspace_state_path=None,
        child_environment_required=False,
        child_environment_applied=None,
    )

    assert workspace.cwd == tmp_path
    assert workspace.invocation_source.source_kind == "project"
    assert workspace.worktree_contract.mode == "blob_exact"


def test_invocation_context_preserves_existing_positional_contract() -> None:
    context = InvocationContext("node", "task", "codex", "executor", 2, 3, True)

    assert context.audit_round_num == 2
    assert context.round_num == 3
    assert context.findings_enabled is True
    assert context.requested_reasoning is None


@pytest.mark.parametrize("report_count", [0, 3])
def test_invocation_usage_serializes_provider_report_count(report_count: int) -> None:
    usage = InvocationUsage(
        attempt_count=1,
        cli_captured=True,
        output_extraction_status="success",
        provider_usage_status="none",
        provider_usage_report_count=report_count,
        provider_tokens={},
        visible_estimate_tokens=None,
        visible_estimate_method=None,
        visible_estimate_is_lower_bound=False,
        configured_cost_usd=None,
        invocation_cost_confidence="none",
        usage_parse_error=None,
    )

    assert usage.as_event_fields()["provider_usage_report_count"] == report_count


@pytest.mark.parametrize("report_count", [-1, True, "1", 1.5])
def test_invocation_usage_rejects_invalid_provider_report_count(
    report_count: object,
) -> None:
    with pytest.raises(
        ValueError,
        match="provider_usage_report_count must be a non-negative integer",
    ):
        InvocationUsage(
            attempt_count=1,
            cli_captured=True,
            output_extraction_status="success",
            provider_usage_status="none",
            provider_usage_report_count=report_count,  # type: ignore[arg-type]
            provider_tokens={},
            visible_estimate_tokens=None,
            visible_estimate_method=None,
            visible_estimate_is_lower_bound=False,
            configured_cost_usd=None,
            invocation_cost_confidence="none",
            usage_parse_error=None,
        )


def test_invocation_usage_preserves_existing_keyword_contract() -> None:
    usage = InvocationUsage(
        attempt_count=1,
        cli_captured=True,
        output_extraction_status="success",
        provider_usage_status="none",
        provider_tokens={},
        visible_estimate_tokens=None,
        visible_estimate_method=None,
        visible_estimate_is_lower_bound=False,
        configured_cost_usd=None,
        invocation_cost_confidence="none",
        usage_parse_error=None,
    )

    assert usage.provider_usage_report_count is None


@pytest.mark.parametrize("report_count", [None, 0, 3])
def test_invocation_event_serializes_nullable_provider_report_count(
    report_count: int | None,
) -> None:
    event = invocation_event(
        event_type="invocation_finished",
        workflow_name="workflow",
        run_id="run-1",
        context=ExecutionEventContext(
            workflow_name="workflow",
            run_id="run-1",
            node_id="node.a",
            provider="codex",
            role=ProviderRole.EXECUTOR,
            task_id="codex_executor_0",
        ),
        provider_usage_report_count=report_count,
    )

    record = execution_event_log_record(event)
    if report_count is None:
        assert "provider_usage_report_count" not in record
    else:
        assert record["provider_usage_report_count"] == report_count
