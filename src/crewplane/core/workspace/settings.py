from __future__ import annotations

from pathlib import Path
from typing import Final, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

from crewplane.core.workflow.keywords import validate_exact_keyword

from .policy import (
    WORKSPACE_CLEAN_START_SET,
    WORKSPACE_CLEAN_START_VALUES,
    WORKTREE_CONTRACT_MODE_SET,
    WORKTREE_CONTRACT_MODES,
    WorkspaceCleanStart,
    WorktreeContractMode,
)

DEFAULT_WORKSPACE_SETUP_TIMEOUT_SECONDS: Final = 600.0


class WorkspaceIdentitySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_cache_root: StrictBool = False


class WorkspaceSetupProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run: list[list[str]] = Field(default_factory=list)

    @field_validator("run", mode="before")
    @classmethod
    def _validate_run_payload(cls, value: object) -> object:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("workspace setup profile run must be a list of argv lists")
        for command in value:
            if not isinstance(command, list):
                raise ValueError(
                    "workspace setup profile commands must be argv lists, not shell strings"
                )
        return value

    @field_validator("run")
    @classmethod
    def _validate_commands(cls, value: list[list[str]]) -> list[list[str]]:
        if not value:
            raise ValueError(
                "workspace setup profile run must contain at least one command"
            )
        for command in value:
            if not command:
                raise ValueError("workspace setup profile commands cannot be empty")
            if any(not token.strip() for token in command):
                raise ValueError(
                    "workspace setup profile commands cannot contain blank tokens"
                )
        return value

    @model_validator(mode="after")
    def _validate_profile_has_commands(self) -> Self:
        if not self.run:
            raise ValueError(
                "workspace setup profile run must contain at least one command"
            )
        return self


class WorkspaceDiskGuardrails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    warn_free_bytes: int | None = Field(default=None, ge=0)
    fail_free_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_threshold_order(self) -> Self:
        if (
            self.warn_free_bytes is not None
            and self.fail_free_bytes is not None
            and self.fail_free_bytes > self.warn_free_bytes
        ):
            raise ValueError(
                "settings.workspace.disk.fail_free_bytes cannot exceed warn_free_bytes"
            )
        return self


class WorkspaceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool = False
    cache_root: str | None = None
    cleanup_on_success: StrictBool = True
    worktree_contract: WorktreeContractMode = "blob_exact"
    clean_start: WorkspaceCleanStart = "strict"
    setup_profiles: dict[str, WorkspaceSetupProfile] = Field(default_factory=dict)
    setup_timeout_seconds: float = Field(
        default=DEFAULT_WORKSPACE_SETUP_TIMEOUT_SECONDS,
        gt=0,
    )
    identity: WorkspaceIdentitySettings = Field(
        default_factory=WorkspaceIdentitySettings
    )
    max_concurrent_materializations: int = Field(default=1, ge=1)
    disk: WorkspaceDiskGuardrails = Field(default_factory=WorkspaceDiskGuardrails)

    @field_validator("worktree_contract", mode="before")
    @classmethod
    def _validate_worktree_contract(cls, value: object) -> object:
        return validate_exact_keyword(
            value,
            field_name="settings.workspace.worktree_contract",
            allowed_values=WORKTREE_CONTRACT_MODES,
            allowed_value_set=WORKTREE_CONTRACT_MODE_SET,
        )

    @field_validator("clean_start", mode="before")
    @classmethod
    def _validate_clean_start(cls, value: object) -> object:
        return validate_exact_keyword(
            value,
            field_name="settings.workspace.clean_start",
            allowed_values=WORKSPACE_CLEAN_START_VALUES,
            allowed_value_set=WORKSPACE_CLEAN_START_SET,
        )

    @field_validator("setup_profiles", mode="before")
    @classmethod
    def _validate_setup_profiles(cls, value: object) -> object:
        if value is None:
            return {}
        if not isinstance(value, dict):
            return value
        normalized: dict[str, object] = {}
        for raw_name, profile in value.items():
            if not isinstance(raw_name, str):
                raise ValueError("workspace setup profile names must be strings")
            name = raw_name.strip()
            if not name:
                raise ValueError("workspace setup profile names cannot be blank")
            if name in normalized:
                raise ValueError(f"Duplicate workspace setup profile '{name}'")
            normalized[name] = profile
        return normalized

    @model_validator(mode="after")
    def _validate_cache_root(self) -> Self:
        if self.enabled and self.cache_root is not None:
            try:
                cache_root = Path(self.cache_root).expanduser()
            except RuntimeError as exc:
                raise ValueError(
                    f"settings.workspace.cache_root could not expand user home: {exc}"
                ) from exc
            if not cache_root.is_absolute():
                raise ValueError(
                    "settings.workspace.cache_root must be absolute when "
                    "workspace isolation is enabled"
                )
        return self
