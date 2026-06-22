from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from orchestrator_cli.architecture.contracts import JsonObject
from orchestrator_cli.core.preflight.plan_contract import (
    validate_supported_plan_schema_version,
)

RUN_STATE_SCHEMA_VERSION = 1

RUN_STATUS_RUNNING = "running"
RUN_STATUS_SUCCEEDED = "succeeded"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_CANCELLED = "cancelled"

RunStatus = Literal["running", "succeeded", "failed", "cancelled"]
TerminalRunStatus = Literal["succeeded", "failed", "cancelled"]
ArtifactKind = Literal["output", "findings", "generated_file"]

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _validate_iso_datetime(value: str) -> str:
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("timestamp fields must be ISO 8601 datetimes.") from exc
    return value


class ArtifactDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ArtifactKind
    relative_path: str
    sha256: str
    size_bytes: int = Field(ge=0)

    @field_validator("relative_path")
    @classmethod
    def _validate_relative_path(cls, value: str) -> str:
        parts = value.split("/")
        if (
            not value
            or value.startswith("/")
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError("Artifact descriptor paths must be relative POSIX paths.")
        return value

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            raise ValueError(
                "Artifact descriptor sha256 must be 64 lowercase hex characters."
            )
        return value


class ResumeOrigin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_run_id: str
    source_run_key_name: str
    source_node_id: str
    hydrated_at: str

    @field_validator("hydrated_at")
    @classmethod
    def _validate_hydrated_at(cls, value: str) -> str:
        return _validate_iso_datetime(value)


class NodeState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_state_schema_version: int
    plan_schema_version: str
    workflow_identity: str
    workflow_name: str
    workflow_signature: str
    run_id: str
    run_key_name: str
    node_id: str
    status: Literal["succeeded"] = "succeeded"
    completed_at: str
    artifacts: list[ArtifactDescriptor] = Field(default_factory=list)
    generated_files: list[ArtifactDescriptor] = Field(default_factory=list)
    workspace: JsonObject | None = None
    resume_origin: ResumeOrigin | None = None

    @field_validator("run_state_schema_version")
    @classmethod
    def _validate_state_schema_version(cls, value: int) -> int:
        if value != RUN_STATE_SCHEMA_VERSION:
            raise ValueError(f"Unsupported run state schema version '{value}'.")
        return value

    @field_validator("plan_schema_version")
    @classmethod
    def _validate_plan_schema_version(cls, value: str) -> str:
        return validate_supported_plan_schema_version(value)

    @field_validator("workflow_signature")
    @classmethod
    def _validate_workflow_signature(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            raise ValueError("workflow_signature must be 64 lowercase hex characters.")
        return value

    @field_validator("completed_at")
    @classmethod
    def _validate_completed_at(cls, value: str) -> str:
        return _validate_iso_datetime(value)


class RunManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_state_schema_version: int
    plan_schema_version: str
    workflow_identity: str
    workflow_name: str
    workflow_signature: str
    run_id: str
    run_key_name: str
    started_at: str
    completed_at: str | None = None
    status: RunStatus
    effective_runtime_config_signature: str
    preflight_plan_path: str
    preflight_manifest_path: str
    runtime_config_snapshot_path: str
    runtime_config_snapshot: JsonObject
    workflow_source: str
    composed_workflow: JsonObject
    referenced_workflows: list[dict[str, str]] = Field(default_factory=list)
    workspace: JsonObject | None = None
    resumed_nodes: list[str] = Field(default_factory=list)
    resume_source_run_id: str | None = None
    resume_source_run_key_name: str | None = None
    failure_message: str | None = None
    cancel_reason: str | None = None

    @field_validator("run_state_schema_version")
    @classmethod
    def _validate_state_schema_version(cls, value: int) -> int:
        if value != RUN_STATE_SCHEMA_VERSION:
            raise ValueError(f"Unsupported run state schema version '{value}'.")
        return value

    @field_validator("plan_schema_version")
    @classmethod
    def _validate_plan_schema_version(cls, value: str) -> str:
        return validate_supported_plan_schema_version(value)

    @field_validator("workflow_signature", "effective_runtime_config_signature")
    @classmethod
    def _validate_signature(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            raise ValueError(
                "Persisted signatures must be 64 lowercase hex characters."
            )
        return value

    @field_validator("started_at")
    @classmethod
    def _validate_started_at(cls, value: str) -> str:
        return _validate_iso_datetime(value)

    @field_validator("completed_at")
    @classmethod
    def _validate_completed_at(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_iso_datetime(value)

    @model_validator(mode="after")
    def _validate_terminal_completion(self) -> RunManifest:
        if self.status == RUN_STATUS_RUNNING and self.completed_at is not None:
            raise ValueError("Running manifests cannot have completed_at.")
        if self.status != RUN_STATUS_RUNNING and self.completed_at is None:
            raise ValueError("Terminal manifests require completed_at.")
        if self.status != RUN_STATUS_FAILED and self.failure_message is not None:
            raise ValueError("failure_message is only valid for failed runs.")
        if self.status != RUN_STATUS_CANCELLED and self.cancel_reason is not None:
            raise ValueError("cancel_reason is only valid for cancelled runs.")
        return self
