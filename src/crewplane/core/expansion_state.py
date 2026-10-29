"""Durable identity and artifact evidence for one committed foreach expansion."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from crewplane.architecture.safe_files import is_safe_relative_path
from crewplane.core.value_checks import is_sha256


class ExpansionProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    group_id: str = Field(min_length=1)
    digest: str
    ordinal: int | None = Field(default=None, strict=True, ge=0)

    @field_validator("digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not is_sha256(value):
            raise ValueError("Expansion digest must be a sha256")
        return value

    def validate_node(self, node_id: str) -> None:
        valid = (
            node_id == self.group_id
            if self.ordinal is None
            else node_id.startswith(f"{self.group_id}.i{self.ordinal}.")
        )
        if not valid:
            raise ValueError(
                "Expansion provenance does not match concrete node identity"
            )


class ExpansionArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: str
    size_bytes: int = Field(strict=True, ge=0)
    sha256: str

    @field_validator("relative_path")
    @classmethod
    def _path(cls, value: str) -> str:
        if not is_safe_relative_path(value):
            raise ValueError("Expansion artifact path must be a safe relative path")
        return value

    @field_validator("sha256")
    @classmethod
    def _sha256(cls, value: str) -> str:
        if not is_sha256(value):
            raise ValueError("Expansion artifact sha256 is invalid")
        return value


class ExpansionState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_id: str
    workflow_signature: str
    template_digest: str
    digest: str
    collection: ExpansionArtifact
    items: list[ExpansionArtifact]
    caller_artifacts: dict[str, ExpansionArtifact]

    def provenance(self, ordinal: int | None = None) -> ExpansionProvenance:
        return ExpansionProvenance(
            group_id=self.group_id, digest=self.digest, ordinal=ordinal
        )
