from __future__ import annotations

from typing import Literal, TypedDict, get_args

from pydantic import BaseModel, ConfigDict, field_validator

from .workflow.keywords import validate_exact_keyword

PromptSegmentRole = Literal["shared", "executor", "reviewer"]

ALLOWED_PROMPT_SEGMENT_ROLES = get_args(PromptSegmentRole)
ALLOWED_PROMPT_SEGMENT_ROLE_SET = frozenset(ALLOWED_PROMPT_SEGMENT_ROLES)


class PromptSegmentPayload(TypedDict):
    role: PromptSegmentRole
    content: str


class PromptSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: PromptSegmentRole
    content: str

    @field_validator("role", mode="before")
    @classmethod
    def _validate_role(cls, value: object) -> object:
        return validate_exact_keyword(
            value,
            field_name="prompt segment role",
            allowed_values=ALLOWED_PROMPT_SEGMENT_ROLES,
            allowed_value_set=ALLOWED_PROMPT_SEGMENT_ROLE_SET,
        )


def prompt_segment_payload_dict(segment: PromptSegment) -> PromptSegmentPayload:
    return {"role": segment.role, "content": segment.content}


def render_prompt_segments(
    segments: list[PromptSegment], role: PromptSegmentRole
) -> str:
    return "".join(
        segment.content for segment in segments if segment.role in ("shared", role)
    )
