from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from orchestrator_cli.versions import WORKFLOW_SCHEMA_VERSION

from ..token_budget import TokenBudgetOverride
from ..workflow_keywords import (
    ALLOWED_NODE_MODE_SET,
    ALLOWED_NODE_MODES,
    NodeMode,
    validate_exact_keyword,
)
from ..workflow_models import ProviderSpec, WorkflowPayload
from ..workflow_syntax import NODE_ID_PATTERN

PromptMarkerKind = Literal["open", "close"]
PromptMarkerRole = Literal["executor", "reviewer"]
ALLOWED_PROMPT_MARKER_ROLES: tuple[PromptMarkerRole, ...] = ("executor", "reviewer")
ALLOWED_PROMPT_MARKER_ROLE_SET = frozenset(ALLOWED_PROMPT_MARKER_ROLES)


class WorkflowNodeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    mode: NodeMode
    findings: bool = False
    providers: list[str | ProviderSpec] = Field(default_factory=list)
    needs: list[str] = Field(default_factory=list)
    audit_rounds: int | None = None
    depth: int | None = None
    continue_on_failure: bool = False
    failure_threshold: int | None = Field(default=None, ge=0)
    source: str | None = None
    token_budget: TokenBudgetOverride | None = None

    @field_validator("mode", mode="before")
    @classmethod
    def _validate_mode(cls, value: object) -> object:
        return validate_exact_keyword(
            value,
            field_name="node mode",
            allowed_values=ALLOWED_NODE_MODES,
            allowed_value_set=ALLOWED_NODE_MODE_SET,
        )


class WorkflowImportConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    path: str
    alias: str = Field(alias="as")
    with_params: dict[str, str] = Field(default_factory=dict, alias="with")
    input_bindings: dict[str, str] = Field(default_factory=dict, alias="inputs")

    @field_validator("path", mode="before")
    @classmethod
    def _validate_path(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("Workflow import path must be a non-empty string.")
        return normalized

    @field_validator("alias", mode="before")
    @classmethod
    def _validate_alias(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("Workflow import alias must be a non-empty string.")
        if not NODE_ID_PATTERN.fullmatch(normalized):
            raise ValueError(
                "Workflow import alias is invalid. Aliases must match '[a-z0-9._-]+'."
            )
        return normalized

    @field_validator("with_params", mode="before")
    @classmethod
    def _validate_with_params(cls, value: object) -> object:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("Workflow import 'with' value must be a mapping.")
        normalized: dict[str, str] = {}
        for raw_key, raw_param_value in value.items():
            if not isinstance(raw_key, str):
                raise ValueError("Workflow import parameter keys must be strings.")
            key = raw_key.strip()
            if not key:
                raise ValueError(
                    "Workflow import parameter keys must be non-empty strings."
                )
            if not isinstance(raw_param_value, str):
                raise ValueError(
                    f"Workflow import parameter '{raw_key}' must be a string."
                )
            if key in normalized:
                raise ValueError(f"Duplicate workflow import parameter key '{key}'.")
            normalized[key] = raw_param_value
        return normalized

    @field_validator("input_bindings", mode="before")
    @classmethod
    def _validate_input_bindings(cls, value: object) -> object:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("Workflow import 'inputs' value must be a mapping.")
        normalized: dict[str, str] = {}
        for raw_key, raw_binding in value.items():
            if not isinstance(raw_key, str):
                raise ValueError("Workflow import input keys must be strings.")
            key = raw_key.strip()
            if not key:
                raise ValueError(
                    "Workflow import input keys must be non-empty strings."
                )
            if not isinstance(raw_binding, str):
                raise ValueError(f"Workflow import input '{raw_key}' must be a string.")
            if key in normalized:
                raise ValueError(f"Duplicate workflow import input key '{key}'.")
            normalized[key] = raw_binding.strip()
        return normalized


class WorkflowFrontmatter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = WORKFLOW_SCHEMA_VERSION
    name: str
    description: str | None = None
    inputs: dict[str, str] = Field(default_factory=dict)
    nodes: list[WorkflowNodeConfig]
    imports: list[WorkflowImportConfig] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: str) -> str:
        if value != WORKFLOW_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported workflow schema version '{value}'. "
                f"Expected '{WORKFLOW_SCHEMA_VERSION}'."
            )
        return value

    @field_validator("inputs", mode="before")
    @classmethod
    def _validate_inputs(cls, value: object) -> object:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("Workflow 'inputs' value must be a mapping.")
        normalized: dict[str, str] = {}
        for raw_key, raw_node_id in value.items():
            if not isinstance(raw_key, str):
                raise ValueError("Workflow input keys must be strings.")
            key = raw_key.strip()
            if not key:
                raise ValueError("Workflow input keys must be non-empty strings.")
            if not isinstance(raw_node_id, str):
                raise ValueError(
                    f"Workflow input '{raw_key}' must reference a node id string."
                )
            if key in normalized:
                raise ValueError(f"Duplicate workflow input key '{key}'.")
            normalized[key] = raw_node_id.strip()
        return normalized


@dataclass(frozen=True)
class ParsedWorkflowBody:
    node_sections: dict[str, list[str]]
    node_section_content_start_lines: dict[str, list[int]]
    node_section_spans: dict[str, list[dict[str, int]]]
    section_headers: list[str]


@dataclass(frozen=True)
class ParsedWorkflowMarkdown:
    frontmatter: WorkflowFrontmatter
    parsed_body: ParsedWorkflowBody
    payload: WorkflowPayload
    node_source_spans: dict[str, dict[str, int]]
    prompt_segment_spans: dict[str, list[dict[str, int]]]


@dataclass(frozen=True)
class WorkflowValidationSummary:
    nodes_defined: int
    node_sections_found: int
    edges_defined: int


@dataclass(frozen=True)
class MarkdownSection:
    header: str
    header_start_line: int
    content_start_line: int
    content_end_line: int


@dataclass(frozen=True)
class MarkerEvent:
    marker_kind: PromptMarkerKind
    role: PromptMarkerRole
    start_line: int
    end_line: int
