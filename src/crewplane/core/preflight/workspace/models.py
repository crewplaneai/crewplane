from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from crewplane.core.workspace.policy import (
    WorkspaceCleanStart,
    WorkspaceMaterialization,
    WorkspaceSourceKind,
    WorktreeContract,
    WorktreeKind,
)


class WorkspaceSetupCommandRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    argv: list[str]
    command_index: int

    @field_validator("argv")
    @classmethod
    def _validate_argv(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("workspace setup command argv cannot be empty")
        if any(not token.strip() for token in value):
            raise ValueError("workspace setup command argv cannot contain blank tokens")
        return value


class WorkspaceSetupRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_name: str
    commands: list[WorkspaceSetupCommandRecord] = Field(default_factory=list)

    @field_validator("commands")
    @classmethod
    def _validate_commands(
        cls,
        value: list[WorkspaceSetupCommandRecord],
    ) -> list[WorkspaceSetupCommandRecord]:
        if not value:
            raise ValueError("workspace setup record must contain commands")
        return value

    @model_validator(mode="after")
    def _validate_record_has_commands(self) -> Self:
        if not self.commands:
            raise ValueError("workspace setup record must contain commands")
        return self


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
