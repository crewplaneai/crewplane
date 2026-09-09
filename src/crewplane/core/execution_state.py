from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from crewplane.architecture.contracts import JsonObject
from crewplane.core.preflight.plan_contract import (
    validate_supported_plan_schema_version,
)
from crewplane.core.value_checks import is_sha256

RUN_STATE_SCHEMA_VERSION = 1

RUN_STATUS_RUNNING = "running"
RUN_STATUS_SUCCEEDED = "succeeded"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_CANCELLED = "cancelled"

RunStatus = Literal["running", "succeeded", "failed", "cancelled"]
TerminalRunStatus = Literal["succeeded", "failed", "cancelled"]
ArtifactKind = Literal["output", "findings", "generated_file"]


def validate_iso_datetime(value: str) -> str:
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
        if not is_sha256(value):
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

    @field_validator("source_run_id", "source_run_key_name", "source_node_id")
    @classmethod
    def _validate_nonblank_provenance(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Resume provenance fields cannot be blank.")
        return value

    @field_validator("hydrated_at")
    @classmethod
    def _validate_hydrated_at(cls, value: str) -> str:
        return validate_iso_datetime(value)


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

    @field_validator(
        "workflow_identity",
        "workflow_name",
        "run_id",
        "run_key_name",
        "node_id",
    )
    @classmethod
    def _validate_nonblank_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Node state identity fields cannot be blank.")
        return value

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
        if not is_sha256(value):
            raise ValueError("workflow_signature must be 64 lowercase hex characters.")
        return value

    @field_validator("completed_at")
    @classmethod
    def _validate_completed_at(cls, value: str) -> str:
        return validate_iso_datetime(value)

    @model_validator(mode="after")
    def _validate_artifact_descriptor_sets(self) -> NodeState:
        artifact_kinds = [descriptor.kind for descriptor in self.artifacts]
        if len(artifact_kinds) != len(set(artifact_kinds)):
            raise ValueError("Node state artifact kinds must be unique.")
        if any(descriptor.kind == "generated_file" for descriptor in self.artifacts):
            raise ValueError(
                "Generated-file descriptors must be stored in generated_files."
            )
        if any(
            descriptor.kind != "generated_file" for descriptor in self.generated_files
        ):
            raise ValueError(
                "generated_files may contain only generated-file descriptors."
            )
        paths = [
            descriptor.relative_path
            for descriptor in (*self.artifacts, *self.generated_files)
        ]
        if len(paths) != len(set(paths)):
            raise ValueError("Node state artifact paths must be unique.")
        return self


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

    @field_validator(
        "workflow_identity",
        "workflow_name",
        "run_id",
        "run_key_name",
        "preflight_plan_path",
        "preflight_manifest_path",
        "runtime_config_snapshot_path",
        "workflow_source",
    )
    @classmethod
    def _validate_nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Run manifest identity and path fields cannot be blank.")
        return value

    @field_validator("failure_message", "cancel_reason")
    @classmethod
    def _validate_nonblank_reason(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Terminal run reasons cannot be blank.")
        return value

    @field_validator("resumed_nodes")
    @classmethod
    def _validate_resumed_nodes(cls, value: list[str]) -> list[str]:
        if any(not node_id.strip() for node_id in value):
            raise ValueError("resumed_nodes cannot contain blank node ids.")
        return value

    @field_validator("resume_source_run_id", "resume_source_run_key_name")
    @classmethod
    def _validate_resume_source(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Resume source provenance fields cannot be blank.")
        return value

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
        if not is_sha256(value):
            raise ValueError(
                "Persisted signatures must be 64 lowercase hex characters."
            )
        return value

    @field_validator("started_at")
    @classmethod
    def _validate_started_at(cls, value: str) -> str:
        return validate_iso_datetime(value)

    @field_validator("completed_at")
    @classmethod
    def _validate_completed_at(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_iso_datetime(value)

    @model_validator(mode="after")
    def _validate_consistency(self) -> RunManifest:
        self._validate_completion_status()
        self._validate_terminal_reasons()
        self._validate_resume_provenance()
        return self

    def _validate_completion_status(self) -> None:
        if self.status == RUN_STATUS_RUNNING and self.completed_at is not None:
            raise ValueError("Running manifests cannot have completed_at.")
        if self.status != RUN_STATUS_RUNNING and self.completed_at is None:
            raise ValueError("Terminal manifests require completed_at.")

    def _validate_terminal_reasons(self) -> None:
        if self.status != RUN_STATUS_FAILED and self.failure_message is not None:
            raise ValueError("failure_message is only valid for failed runs.")
        if self.status != RUN_STATUS_CANCELLED and self.cancel_reason is not None:
            raise ValueError("cancel_reason is only valid for cancelled runs.")
        if self.status == RUN_STATUS_FAILED and self.failure_message is None:
            raise ValueError("Failed manifests require failure_message.")
        if self.status == RUN_STATUS_CANCELLED and self.cancel_reason is None:
            raise ValueError("Cancelled manifests require cancel_reason.")

    def _validate_resume_provenance(self) -> None:
        has_source_run_id = self.resume_source_run_id is not None
        has_source_run_key = self.resume_source_run_key_name is not None
        if has_source_run_id != has_source_run_key:
            raise ValueError("Resume source provenance must be all-or-none.")
        if bool(self.resumed_nodes) != has_source_run_id:
            raise ValueError(
                "Resume source provenance is required exactly when nodes were hydrated."
            )
        if len(self.resumed_nodes) != len(set(self.resumed_nodes)):
            raise ValueError("resumed_nodes cannot contain duplicates.")
