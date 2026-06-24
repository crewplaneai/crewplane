from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from crewplane.core.token_budget import TokenBudgetOverride
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workspace.policy import WorktreeDeclaration, validate_worktree_name
from crewplane.version import SCHEMA_VERSION

from ..keywords import (
    ALLOWED_NODE_MODE_SET,
    ALLOWED_NODE_MODES,
    ALLOWED_REVIEW_STARTS_WITH,
    ALLOWED_REVIEW_STARTS_WITH_SET,
    NodeMode,
    ReviewStartsWith,
    validate_exact_keyword,
)
from ..models import ProviderSpec, WorkflowPayload
from ..syntax import NODE_ID_PATTERN

PromptMarkerKind = Literal["open", "close"]
type PromptMarkerRole = ProviderRole
ALLOWED_PROMPT_MARKER_ROLES = tuple(role.value for role in ProviderRole)
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
    review_starts_with: ReviewStartsWith = "executor"
    continue_on_failure: bool = False
    failure_threshold: int | None = Field(default=None, ge=0)
    source: str | None = None
    token_budget: TokenBudgetOverride | None = None
    worktree: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_workspace_block(cls, value: object) -> object:
        if isinstance(value, dict) and "workspace" in value:
            raise ValueError(
                "node workspace blocks have been removed; use node worktree selectors"
            )
        return value

    @field_validator("mode", mode="before")
    @classmethod
    def _validate_mode(cls, value: object) -> object:
        return validate_exact_keyword(
            value,
            field_name="node mode",
            allowed_values=ALLOWED_NODE_MODES,
            allowed_value_set=ALLOWED_NODE_MODE_SET,
        )

    @field_validator("review_starts_with", mode="before")
    @classmethod
    def _validate_review_starts_with(cls, value: object) -> object:
        return validate_exact_keyword(
            value,
            field_name="review_starts_with",
            allowed_values=ALLOWED_REVIEW_STARTS_WITH,
            allowed_value_set=ALLOWED_REVIEW_STARTS_WITH_SET,
        )

    @field_validator("worktree", mode="before")
    @classmethod
    def _validate_worktree(cls, value: object) -> object:
        if value is None:
            return value
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("worktree selector cannot be blank")
        return normalized


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
            binding = raw_binding.strip()
            if not binding:
                raise ValueError(
                    f"Workflow import input '{raw_key}' must reference a non-empty node id."
                )
            if key in normalized:
                raise ValueError(f"Duplicate workflow import input key '{key}'.")
            normalized[key] = binding
        return normalized


class WorkflowFrontmatter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    name: str
    description: str | None = None
    inputs: dict[str, str] = Field(default_factory=dict)
    worktrees: dict[str, WorktreeDeclaration] = Field(default_factory=dict)
    nodes: list[WorkflowNodeConfig]
    imports: list[WorkflowImportConfig] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_workspace_block(cls, value: object) -> object:
        if isinstance(value, dict) and "workspace" in value:
            raise ValueError(
                "workflow workspace blocks have been removed; use workflow worktrees"
            )
        return value

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: str) -> str:
        if value != SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported workflow schema version '{value}'. "
                f"Expected '{SCHEMA_VERSION}'."
            )
        return value

    @field_validator("worktrees", mode="before")
    @classmethod
    def _validate_worktrees(cls, value: object) -> object:
        if value is None:
            return {}
        if not isinstance(value, dict):
            return value
        normalized: dict[str, object] = {}
        for raw_name, declaration in value.items():
            if not isinstance(raw_name, str):
                raise ValueError("worktree names must be strings")
            name = validate_worktree_name(raw_name)
            if name in normalized:
                raise ValueError(f"Duplicate worktree name '{name}'")
            normalized[name] = declaration
        return normalized

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
