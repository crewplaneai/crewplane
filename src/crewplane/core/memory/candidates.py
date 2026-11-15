from __future__ import annotations

from markdown_it import MarkdownIt
from pydantic import Field, field_validator, model_validator

from crewplane.version import SCHEMA_VERSION

from .models import FrozenModel, Nonempty


class LessonProposal(FrozenModel):
    title: Nonempty
    body: Nonempty
    tags: tuple[str, ...] = ()
    qualifications: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    corrects_record: str | None = None
    expected_accepted_revision: str | None = None

    @model_validator(mode="after")
    def validate_correction(self) -> LessonProposal:
        if (self.corrects_record is None) != (self.expected_accepted_revision is None):
            raise ValueError(
                "A correction requires both record and accepted-revision identities."
            )
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("Evidence IDs must be unique.")
        return self


class CandidateDocument(FrozenModel):
    schema_version: str
    lessons: tuple[LessonProposal, ...] = Field(max_length=5)

    @field_validator("schema_version")
    @classmethod
    def validate_schema(cls, value: str) -> str:
        if value != SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported candidate schema; expected {SCHEMA_VERSION!r}."
            )
        return value


def parse_candidates(markdown: str) -> CandidateDocument:
    blocks = [
        token.content
        for token in MarkdownIt("commonmark").parse(markdown)
        if token.type == "fence" and token.info.strip() == "crewplane-memory-candidates"
    ]
    if len(blocks) != 1:
        raise ValueError(
            "Expected exactly one fenced crewplane-memory-candidates JSON block."
        )
    return CandidateDocument.model_validate_json(blocks[0])
