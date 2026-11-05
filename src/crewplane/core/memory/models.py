from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from crewplane.version import SCHEMA_VERSION

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Nonempty = Annotated[str, Field(min_length=1)]


def digest_payload(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceReceipt(FrozenModel):
    evidence_id: Nonempty
    kind: Literal[
        "agent_output", "framework_outcome", "provider_process", "captured_check"
    ]
    source_locator: Nonempty
    source_sha256: Digest
    excerpt: str
    excerpt_sha256: Digest
    observed_at: Nonempty
    captured_at: Nonempty
    run_id: Nonempty
    node_id: Nonempty
    qualifications: tuple[str, ...] = ()
    provenance: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_excerpt(self) -> EvidenceReceipt:
        if (
            hashlib.sha256(self.excerpt.encode("utf-8")).hexdigest()
            != self.excerpt_sha256
        ):
            raise ValueError(
                "Retained evidence excerpt digest does not match its bytes."
            )
        return self

    def semantic_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"observed_at", "captured_at"})


class ProposalProvenance(FrozenModel):
    author: Literal["human", "extractor"]
    source_identity: Nonempty
    run_id: str | None = None
    extraction_run_id: str | None = None


class Acceptance(FrozenModel):
    accepted_at: Nonempty
    candidate_digest: Digest
    expected_accepted_revision: str | None = None


class MemoryRevision(FrozenModel):
    revision_id: Nonempty
    title: Nonempty
    body: Nonempty
    tags: tuple[str, ...] = ()
    qualifications: tuple[str, ...] = ()
    evidence: tuple[EvidenceReceipt, ...] = ()
    provenance: ProposalProvenance
    proposal_identity: Digest
    expected_accepted_revision: str | None = None
    content_digest: Digest
    evidence_digest: Digest
    candidate_digest: Digest
    disposition: Literal["pending", "accepted", "rejected", "superseded"] = "pending"
    created_at: Nonempty
    acceptance: Acceptance | None = None

    def content_payload(self) -> dict[str, object]:
        return {
            "title": self.title,
            "body": self.body,
            "tags": list(self.tags),
            "qualifications": list(self.qualifications),
        }

    def identity_payload(self) -> dict[str, object]:
        return {
            "content_digest": self.content_digest,
            "evidence_digest": self.evidence_digest,
            "provenance": self.provenance.model_dump(mode="json"),
            "expected_accepted_revision": self.expected_accepted_revision,
        }

    @model_validator(mode="after")
    def validate_identities(self) -> MemoryRevision:
        if self.content_digest != digest_payload(self.content_payload()):
            raise ValueError("Memory content digest does not match revision content.")
        if self.evidence_digest != digest_payload(
            [item.semantic_payload() for item in self.evidence]
        ):
            raise ValueError("Memory evidence digest does not match retained evidence.")
        if self.candidate_digest != digest_payload(self.identity_payload()):
            raise ValueError(
                "Memory candidate digest does not match revision identity."
            )
        if self.disposition == "accepted" and self.acceptance is None:
            raise ValueError("Accepted revision requires acceptance metadata.")
        if self.acceptance is not None and (
            self.disposition not in {"accepted", "superseded"}
            or self.acceptance.candidate_digest != self.candidate_digest
            or self.acceptance.expected_accepted_revision
            != self.expected_accepted_revision
        ):
            raise ValueError("Acceptance metadata disagrees with its revision.")
        return self


class MemoryRecord(FrozenModel):
    record_id: Nonempty
    revisions: tuple[MemoryRevision, ...] = Field(min_length=1)
    accepted_revision: str | None = None

    @model_validator(mode="after")
    def validate_current_revision(self) -> MemoryRecord:
        ids = {revision.revision_id for revision in self.revisions}
        if len(ids) != len(self.revisions):
            raise ValueError("Memory revision identifiers must be unique.")
        accepted = [
            r.revision_id for r in self.revisions if r.disposition == "accepted"
        ]
        if accepted != (
            [] if self.accepted_revision is None else [self.accepted_revision]
        ):
            raise ValueError(
                "Memory accepted pointer must identify its sole accepted revision."
            )
        for revision in self.revisions:
            if (
                revision.expected_accepted_revision is not None
                and revision.expected_accepted_revision not in ids
            ):
                raise ValueError("Memory correction predecessor is missing.")
        return self

    @property
    def state_digest(self) -> str:
        return digest_payload(self.model_dump(mode="json"))


class MemorySnapshot(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    transaction_id: str = "initial"
    records: tuple[MemoryRecord, ...] = ()

    @field_validator("schema_version")
    @classmethod
    def validate_schema(cls, value: str) -> str:
        if value != SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported memory schema {value!r}; expected {SCHEMA_VERSION!r}."
            )
        return value

    @model_validator(mode="after")
    def validate_unique_records(self) -> MemorySnapshot:
        if len({record.record_id for record in self.records}) != len(self.records):
            raise ValueError("Memory record identifiers must be unique.")
        proposals = [
            revision.proposal_identity
            for record in self.records
            for revision in record.revisions
        ]
        if len(set(proposals)) != len(proposals):
            raise ValueError("Memory proposal identities must be unique.")
        return self
