from __future__ import annotations

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from crewplane.core.execution_state import (
    RUN_STATE_SCHEMA_VERSION,
    validate_iso_datetime,
)
from crewplane.core.workflow.keywords import ProviderRole

ProviderProcessStatus = Literal["started", "exited"]


class ProviderProcessState(BaseModel):
    """Durable identity for one built-in CLI invoker process attempt."""

    model_config = ConfigDict(extra="forbid")

    run_state_schema_version: int
    run_id: str
    run_key_name: str
    node_id: str
    task_id: str
    provider: str
    role: ProviderRole
    audit_round_num: int | None = Field(default=None, ge=1)
    round_num: int = Field(ge=0)
    attempt: int = Field(ge=1)
    pid: int = Field(gt=0)
    process_group_id: int | None = Field(default=None, gt=0)
    hostname: str
    process_start_identity: str | None = None
    status: ProviderProcessStatus
    started_at: str
    exited_at: str | None = None
    returncode: int | None = None

    @field_validator("run_state_schema_version")
    @classmethod
    def _validate_state_schema_version(cls, value: int) -> int:
        if value != RUN_STATE_SCHEMA_VERSION:
            raise ValueError(f"Unsupported run state schema version '{value}'.")
        return value

    @field_validator(
        "run_id",
        "run_key_name",
        "node_id",
        "task_id",
        "provider",
        "hostname",
    )
    @classmethod
    def _validate_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("provider process identity fields must not be empty.")
        return value

    @field_validator("started_at")
    @classmethod
    def _validate_started_at(cls, value: str) -> str:
        return validate_iso_datetime(value)

    @field_validator("exited_at")
    @classmethod
    def _validate_exited_at(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_iso_datetime(value)

    @model_validator(mode="after")
    def _validate_terminal_fields(self) -> ProviderProcessState:
        if self.status == "started":
            if self.exited_at is not None or self.returncode is not None:
                raise ValueError(
                    "Started provider process records cannot contain exit fields."
                )
            return self
        if self.exited_at is None or self.returncode is None:
            raise ValueError(
                "Exited provider process records require exit time and return code."
            )
        return self
