from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from crewplane.architecture.contracts import JsonObject
from crewplane.core.workspace.policy import (
    WorkspaceCleanStart,
    WorkspaceMaterialization,
    WorkspaceSourceKind,
    WorktreeContract,
    WorktreeKind,
)

_REDACTED_TOKEN_FIELDS = {"fingerprint", "redacted", "value_handle"}


def _validate_setup_command_token(token: str | JsonObject) -> None:
    if isinstance(token, str):
        _validate_setup_command_text_token(token)
        return
    _validate_setup_command_redacted_token(token)


def _validate_setup_command_text_token(token: str) -> None:
    if not token.strip():
        raise ValueError("workspace setup command argv cannot contain blank tokens")


def _validate_setup_command_redacted_token(token: JsonObject) -> None:
    if (
        token.get("redacted") is not True
        or not isinstance(token.get("value_handle"), str)
        or set(token) - _REDACTED_TOKEN_FIELDS
    ):
        raise ValueError(
            "workspace setup command argv contains an invalid redacted token"
        )


class WorkspaceSetupCommandRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    argv: list[str | JsonObject]
    command_index: int

    @field_validator("argv")
    @classmethod
    def _validate_argv(
        cls,
        value: list[str | JsonObject],
    ) -> list[str | JsonObject]:
        if not value:
            raise ValueError("workspace setup command argv cannot be empty")
        for token in value:
            _validate_setup_command_token(token)
        return value


class WorkspaceSetupRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_name: str
    commands: list[WorkspaceSetupCommandRecord] = Field(
        default_factory=list,
        validate_default=True,
    )

    @field_validator("commands")
    @classmethod
    def _require_commands(
        cls,
        value: list[WorkspaceSetupCommandRecord],
    ) -> list[WorkspaceSetupCommandRecord]:
        if not value:
            raise ValueError("workspace setup record must contain commands")
        return value


class WorkspaceBranchExportRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    create_branch: bool = False
    branch_name: str | None = None


class WorkspaceSelectionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    logical_worktree_name: str | None = None
    declaration_kind: WorktreeKind | None = None
    source_kind: WorkspaceSourceKind = "project"
    source_node_id: str | None = None
    clean_start: WorkspaceCleanStart = "strict"
    materialization: WorkspaceMaterialization = "project_root"
    worktree_contract: WorktreeContract = Field(default_factory=WorktreeContract)
    setup: WorkspaceSetupRecord | None = None
    branch_export: WorkspaceBranchExportRecord = Field(
        default_factory=WorkspaceBranchExportRecord
    )
    writable: bool = False
    lineage_producer: bool = False

    @model_validator(mode="after")
    def _validate_workspace_contract(self) -> Self:
        if not self.enabled:
            _validate_disabled_workspace_selection(self)
            return self
        _validate_enabled_workspace_declaration(self)
        _validate_enabled_workspace_source(self)
        _validate_workspace_declaration_kind(self)
        if not self.writable:
            raise ValueError("managed workspace selection must be writable")
        _validate_workspace_lineage(self)
        return self


def _validate_disabled_workspace_selection(selection: WorkspaceSelectionRecord) -> None:
    if selection.materialization != "project_root":
        raise ValueError("disabled workspace selection must use project_root")


def _validate_enabled_workspace_declaration(
    selection: WorkspaceSelectionRecord,
) -> None:
    if selection.logical_worktree_name is None or selection.declaration_kind is None:
        raise ValueError("enabled workspace selection requires a declaration")


def _validate_enabled_workspace_source(selection: WorkspaceSelectionRecord) -> None:
    if selection.source_kind == "node" and selection.source_node_id is None:
        raise ValueError("node workspace source requires source_node_id")
    if selection.source_kind != "node" and selection.source_node_id is not None:
        raise ValueError("only node workspace sources may name source_node_id")


def _validate_workspace_declaration_kind(
    selection: WorkspaceSelectionRecord,
) -> None:
    expected_materialization = (
        "snapshot_checkout"
        if selection.declaration_kind == "snapshot"
        else "worktree_checkout"
    )
    if selection.materialization != expected_materialization:
        raise ValueError("workspace kind and materialization are inconsistent")


def _validate_workspace_lineage(selection: WorkspaceSelectionRecord) -> None:
    if selection.declaration_kind == "snapshot" and selection.lineage_producer:
        raise ValueError("snapshot workspace cannot produce lineage")


class WorkspaceSourceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worktree_contract: WorktreeContract
    run_base_commit: str
    source_tree: str
    object_format: str
    repository_id: str
    git_version: str
    git_top_level: str
    project_root_relative_path: str
    active_git_dir: str
    common_git_dir: str
    clean_start: str
    local_config_policy: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    filesystem_capabilities: dict[str, bool] = Field(default_factory=dict)
