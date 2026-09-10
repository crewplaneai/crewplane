from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from crewplane.core.config import (
    AgentConfig,
    Config,
    FileAccessSettings,
    IntegrationSpec,
)
from crewplane.core.execution_state import (
    ArtifactDescriptor,
    NodeState,
    ResumeOrigin,
    RunManifest,
)
from crewplane.core.preflight.models import (
    Fragment,
    WorkspaceSelectionRecord,
    WorkspaceSetupCommandRecord,
)
from crewplane.core.workspace.policy import WorktreeDeclaration, validate_branch_name
from crewplane.core.workspace.settings import WorkspaceSettings, WorkspaceSetupProfile
from crewplane.version import SCHEMA_VERSION
from tests.helpers.resume import make_node_state, make_run_manifest


@pytest.mark.parametrize(
    ("model", "payload", "message"),
    [
        (AgentConfig, {"cli_cmd": ["echo"], "provider_kind": 1}, "provider_kind"),
        (AgentConfig, {"cli_cmd": ["echo"], "prompt_transport": 1}, "prompt_transport"),
        (
            AgentConfig,
            {"cli_cmd": ["echo"], "prompt_transport": "unknown"},
            "prompt_transport must be",
        ),
        (
            AgentConfig,
            {"cli_cmd": ["echo"], "retry_on_exit_codes": [-1]},
            "exit codes must be",
        ),
        (
            AgentConfig,
            {"cli_cmd": ["echo"], "retry_on_exit_codes": [256]},
            "exit codes must be",
        ),
        (IntegrationSpec, {"implementation": 1}, "implementation"),
        (FileAccessSettings, {"allowed_template_paths": [" "]}, "blank paths"),
        (Config, {"version": SCHEMA_VERSION, "agents": []}, "agents"),
        (
            Config,
            {"version": SCHEMA_VERSION, "agents": {1: {"cli_cmd": ["echo"]}}},
            "agent names must be strings",
        ),
        (
            WorktreeDeclaration,
            {"kind": "worktree", "setup_profile": 1},
            "setup_profile",
        ),
        (
            WorktreeDeclaration,
            {"kind": "worktree", "setup_profile": " "},
            "setup_profile cannot be blank",
        ),
        (WorktreeDeclaration, {"kind": "worktree", "branch_name": 1}, "branch_name"),
        (
            WorktreeDeclaration,
            {"kind": "snapshot", "setup_profile": "install"},
            "snapshot worktrees cannot declare setup_profile",
        ),
        (
            WorktreeDeclaration,
            {"kind": "snapshot", "create_branch": True},
            "snapshot worktrees cannot declare create_branch",
        ),
        (
            WorktreeDeclaration,
            {"kind": "snapshot", "branch_name": "feature"},
            "snapshot worktrees cannot declare branch_name",
        ),
        (WorkspaceSetupProfile, {"run": None}, "at least one command"),
        (WorkspaceSetupProfile, {"run": "echo"}, "list of argv lists"),
        (WorkspaceSettings, {"setup_profiles": []}, "setup_profiles"),
        (
            WorkspaceSettings,
            {"setup_profiles": {1: {"run": [["echo"]]}}},
            "names must be strings",
        ),
        (
            WorkspaceSettings,
            {"setup_profiles": {" ": {"run": [["echo"]]}}},
            "cannot be blank",
        ),
        (
            WorkspaceSettings,
            {
                "setup_profiles": {
                    " install ": {"run": [["echo"]]},
                    "install": {"run": [["echo"]]},
                }
            },
            "Duplicate workspace setup profile",
        ),
        (
            WorkspaceSetupCommandRecord,
            {"argv": [], "command_index": 0},
            "argv cannot be empty",
        ),
        (
            WorkspaceSetupCommandRecord,
            {
                "argv": [{"redacted": False, "value_handle": "secret"}],
                "command_index": 0,
            },
            "invalid redacted token",
        ),
        (
            WorkspaceSetupCommandRecord,
            {"argv": [{"redacted": True, "value_handle": 1}], "command_index": 0},
            "invalid redacted token",
        ),
        (
            WorkspaceSetupCommandRecord,
            {
                "argv": [
                    {"redacted": True, "value_handle": "secret", "unexpected": True}
                ],
                "command_index": 0,
            },
            "invalid redacted token",
        ),
        (
            ArtifactDescriptor,
            {
                "kind": "output",
                "relative_path": "output.md",
                "sha256": "z" * 64,
                "size_bytes": 0,
            },
            "lowercase hex",
        ),
    ],
)
def test_configuration_and_persisted_record_boundaries_reject_invalid_payloads(
    model: type[BaseModel], payload: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        model.model_validate(payload)


@pytest.mark.parametrize(
    "name",
    [
        "",
        " padded",
        "HEAD",
        "-option",
        "feature..name",
        "refs/heads/feature",
        "feature.lock",
        "feature//name",
    ],
)
def test_branch_names_reject_unsafe_reference_forms(name: str) -> None:
    with pytest.raises(ValueError, match="branch_name"):
        validate_branch_name(name)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"writable": False}, "must be writable"),
        ({"enabled": False}, "disabled workspace selection must use project_root"),
        ({"source_kind": "node"}, "requires source_node_id"),
        ({"source_node_id": "previous"}, "only node workspace sources"),
        (
            {"materialization": "snapshot_checkout"},
            "kind and materialization are inconsistent",
        ),
        (
            {"declaration_kind": "snapshot", "materialization": "snapshot_checkout"},
            "snapshot workspace cannot produce lineage",
        ),
    ],
)
def test_workspace_selection_rejects_inconsistent_persisted_contract(
    changes: dict[str, object], message: str
) -> None:
    payload = {
        "enabled": True,
        "logical_worktree_name": "main",
        "declaration_kind": "worktree",
        "materialization": "worktree_checkout",
        "writable": True,
        "lineage_producer": True,
    }
    WorkspaceSelectionRecord.model_validate(payload)
    payload.update(changes)
    with pytest.raises(ValidationError, match=message):
        WorkspaceSelectionRecord.model_validate(payload)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"kind": "literal"}, "Literal fragments require text"),
        ({"kind": "static_file_content"}, "Static file fragments require content_ref"),
        (
            {"kind": "workspace_file_locator"},
            "Workspace file locator fragments require locator",
        ),
        (
            {"kind": "runtime_locator_lookup"},
            "Runtime locator fragments require locator",
        ),
        (
            {"kind": "static_env", "value_stored": "value"},
            "Static value fragments require key",
        ),
        (
            {
                "kind": "static_var",
                "key": "name",
                "value_stored": "value",
                "fingerprint": "digest",
            },
            "must not define fingerprint",
        ),
    ],
)
def test_render_fragments_reject_missing_or_conflicting_payloads(
    fields: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        Fragment.model_validate(
            {"fragment_index": 0, "source_role": "shared", **fields}
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"workflow_name": " "}, "identity fields cannot be blank"),
        ({"run_state_schema_version": 999}, "Unsupported run state schema"),
        ({"workflow_signature": "invalid"}, "workflow_signature must be"),
        (
            {
                "artifacts": [
                    {
                        "kind": "generated_file",
                        "relative_path": "generated.txt",
                        "sha256": "a" * 64,
                        "size_bytes": 1,
                    }
                ]
            },
            "must be stored in generated_files",
        ),
        (
            {
                "generated_files": [
                    {
                        "kind": "output",
                        "relative_path": "output.md",
                        "sha256": "a" * 64,
                        "size_bytes": 1,
                    }
                ]
            },
            "only generated-file descriptors",
        ),
        (
            {
                "generated_files": [
                    {
                        "kind": "generated_file",
                        "relative_path": "same.txt",
                        "sha256": "a" * 64,
                        "size_bytes": 1,
                    }
                ]
                * 2
            },
            "artifact paths must be unique",
        ),
    ],
)
def test_node_state_rejects_ambiguous_or_invalid_resume_evidence(
    changes: dict[str, object], message: str
) -> None:
    manifest = make_run_manifest("run", "workflow--run", status="succeeded")
    payload = make_node_state(manifest, "a", []).model_dump(mode="json")
    payload.update(changes)
    with pytest.raises(ValidationError, match=message):
        NodeState.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("workflow_source", " ", "identity and path fields cannot be blank"),
        ("resumed_nodes", [" "], "cannot contain blank node ids"),
        ("resume_source_run_id", " ", "provenance fields cannot be blank"),
    ],
)
def test_run_manifest_rejects_blank_audit_evidence(
    field: str, value: object, message: str
) -> None:
    payload = make_run_manifest("run", "workflow--run").model_dump(mode="json")
    payload[field] = value
    with pytest.raises(ValidationError, match=message):
        RunManifest.model_validate(payload)


def test_resume_origin_rejects_blank_source_identity() -> None:
    with pytest.raises(ValidationError, match="provenance fields cannot be blank"):
        ResumeOrigin(
            source_run_id=" ",
            source_run_key_name="source",
            source_node_id="a",
            hydrated_at="2026-01-01T00:00:00",
        )


def test_absent_workspace_setup_profiles_are_empty() -> None:
    assert (
        WorkspaceSettings.model_validate({"setup_profiles": None}).setup_profiles == {}
    )
