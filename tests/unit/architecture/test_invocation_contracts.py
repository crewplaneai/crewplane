from __future__ import annotations

import pytest

from crewplane.architecture.contracts import (
    InvocationSourceContext,
    InvocationWorkspaceContext,
    InvocationWorktreeContract,
    InvokerAdapterCapabilities,
    InvokerWorkspaceSupport,
    LogPresentationDescriptor,
    normalize_log_presentation_profile,
    validate_log_presentation_descriptor,
)
from crewplane.version import SCHEMA_VERSION


def test_log_presentation_profile_normalizes_safe_unknown_profiles() -> None:
    assert (
        normalize_log_presentation_profile(" Vendor.Profile-1 ") == "vendor.profile-1"
    )


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
