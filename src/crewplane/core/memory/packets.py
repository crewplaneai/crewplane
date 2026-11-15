from __future__ import annotations

from typing import Literal

from pydantic import field_validator, model_validator

from crewplane.version import SCHEMA_VERSION

from .models import Digest, EvidenceReceipt, FrozenModel, Nonempty, digest_payload


class EvidencePacket(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    parent_run_id: Nonempty
    parent_run_key: Nonempty
    workflow_signature: Digest
    parent_outcome: Literal["succeeded", "failed"]
    captured_at: Nonempty
    qualifications: tuple[str, ...]
    evidence: tuple[EvidenceReceipt, ...]
    omissions: tuple[str, ...] = ()
    packet_digest: Digest

    @field_validator("schema_version")
    @classmethod
    def validate_schema(cls, value: str) -> str:
        if value != SCHEMA_VERSION:
            raise ValueError("Unsupported memory evidence packet schema.")
        return value

    def identity_payload(self) -> dict[str, object]:
        return {
            "parent_run_id": self.parent_run_id,
            "parent_run_key": self.parent_run_key,
            "workflow_signature": self.workflow_signature,
            "parent_outcome": self.parent_outcome,
            "qualifications": self.qualifications,
            "evidence": [item.semantic_payload() for item in self.evidence],
            "omissions": self.omissions,
        }

    @model_validator(mode="after")
    def validate_identity(self) -> EvidencePacket:
        if self.packet_digest != digest_payload(self.identity_payload()):
            raise ValueError(
                "Memory evidence packet identity does not match its retained contents."
            )
        if len({item.evidence_id for item in self.evidence}) != len(self.evidence):
            raise ValueError("Host evidence IDs must be unique.")
        if any(item.run_id != self.parent_run_id for item in self.evidence):
            raise ValueError("Retained evidence must belong to the parent run.")
        return self
