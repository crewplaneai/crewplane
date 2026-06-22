from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import NotRequired, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from orchestrator_cli.version import SCHEMA_VERSION

from .prompt_segments import (
    PromptSegment,
    PromptSegmentPayload,
    PromptSegmentRole,
    prompt_segment_payload_dict,
    render_prompt_segments,
)
from .provider_names import normalize_provider_name
from .token_budget import (
    TokenBudgetOverride,
    TokenBudgetPayload,
    token_budget_payload_dict,
)
from .workflow_keywords import (
    ALLOWED_NODE_MODE_SET,
    ALLOWED_NODE_MODES,
    ALLOWED_PROVIDER_ROLE_SET,
    ALLOWED_PROVIDER_ROLES,
    NodeMode,
    ProviderRole,
    validate_exact_keyword,
)
from .workflow_syntax import INPUT_SOURCE_PATTERN
from .workspace_policy import (
    PROJECT_ROOT_WORKTREE_SELECTOR,
    WorktreeDeclaration,
    validate_worktree_name,
    worktree_declarations_payload,
)


class ProviderSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str | None = None
    role: ProviderRole = "executor"

    @field_validator("provider", mode="before")
    @classmethod
    def _validate_provider(cls, value: object) -> object:
        return normalize_provider_name(value, "provider")

    @field_validator("role", mode="before")
    @classmethod
    def _validate_role(cls, value: object) -> object:
        return validate_exact_keyword(
            value,
            field_name="provider role",
            allowed_values=ALLOWED_PROVIDER_ROLES,
            allowed_value_set=ALLOWED_PROVIDER_ROLE_SET,
        )


class WorkflowProviderPayload(TypedDict):
    provider: str
    model: NotRequired[str]
    role: NotRequired[ProviderRole]


class WorkflowNodePayload(TypedDict):
    id: str
    mode: NodeMode
    providers: list[WorkflowProviderPayload]
    needs: list[str]
    continue_on_failure: bool
    findings: NotRequired[bool]
    prompt_segments: NotRequired[list[PromptSegmentPayload]]
    source: NotRequired[str]
    depth: NotRequired[int]
    audit_rounds: NotRequired[int]
    failure_threshold: NotRequired[int]
    token_budget: NotRequired[TokenBudgetPayload]
    worktree: NotRequired[str]


WorkflowImportPayload = TypedDict(
    "WorkflowImportPayload",
    {
        "path": str,
        "as": str,
        "with": dict[str, str],
        "inputs": dict[str, str],
    },
)


class WorkflowPayload(TypedDict):
    schema_version: str
    name: str
    description: str
    inputs: dict[str, str]
    worktrees: NotRequired[dict[str, dict[str, str | bool]]]
    nodes: list[WorkflowNodePayload]
    imports: NotRequired[list[WorkflowImportPayload]]


class WorkflowNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    mode: NodeMode
    findings: bool = False
    prompt_segments: list[PromptSegment] = Field(default_factory=list)
    source: str | None = None
    providers: list[ProviderSpec] = Field(default_factory=list)
    needs: list[str] = Field(default_factory=list)
    audit_rounds: int | None = None
    depth: int | None = None
    continue_on_failure: bool = False
    failure_threshold: int | None = None
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

    @model_validator(mode="after")
    def _validate_input_worktree_selector(self) -> WorkflowNode:
        if self.mode == "input" and "worktree" in self.model_fields_set:
            raise ValueError("input nodes cannot declare worktree selectors")
        return self


@dataclass(frozen=True)
class InputNodeContractRule:
    field_name: str
    is_defined: Callable[[WorkflowNode], bool]


INPUT_NODE_CONTRACT_RULES = (
    InputNodeContractRule(
        "prompt_segments",
        lambda node: bool(node.prompt_segments),
    ),
    InputNodeContractRule("findings", lambda node: node.findings),
    InputNodeContractRule("providers", lambda node: bool(node.providers)),
    InputNodeContractRule("dependencies", lambda node: bool(node.needs)),
    InputNodeContractRule("depth", lambda node: node.depth is not None),
    InputNodeContractRule("audit_rounds", lambda node: node.audit_rounds is not None),
    InputNodeContractRule(
        "failure_threshold", lambda node: node.failure_threshold is not None
    ),
    InputNodeContractRule("continue_on_failure", lambda node: node.continue_on_failure),
    InputNodeContractRule("token_budget", lambda node: node.token_budget is not None),
)


def workflow_provider_payload_dict(provider: ProviderSpec) -> WorkflowProviderPayload:
    provider_payload: WorkflowProviderPayload = {"provider": provider.provider}
    if provider.model is not None:
        provider_payload["model"] = provider.model
    if provider.role != "executor":
        provider_payload["role"] = provider.role
    return provider_payload


def workflow_node_payload_dict(node: WorkflowNode) -> WorkflowNodePayload:
    node_payload: WorkflowNodePayload = {
        "id": node.id,
        "mode": node.mode,
        "providers": [
            workflow_provider_payload_dict(provider) for provider in node.providers
        ],
        "needs": list(node.needs),
        "continue_on_failure": node.continue_on_failure,
    }
    if node.findings:
        node_payload["findings"] = True
    if node.mode == "input":
        node_payload["source"] = node.source or ""
    else:
        node_payload["prompt_segments"] = [
            prompt_segment_payload_dict(segment) for segment in node.prompt_segments
        ]
    if node.depth is not None:
        node_payload["depth"] = node.depth
    if node.audit_rounds is not None:
        node_payload["audit_rounds"] = node.audit_rounds
    if node.failure_threshold is not None:
        node_payload["failure_threshold"] = node.failure_threshold
    if node.token_budget is not None:
        node_payload["token_budget"] = token_budget_payload_dict(node.token_budget)
    if node.worktree is not None:
        node_payload["worktree"] = node.worktree
    return node_payload


def workflow_payload_dict(workflow: WorkflowPlan) -> WorkflowPayload:
    payload: WorkflowPayload = {
        "schema_version": workflow.schema_version,
        "name": workflow.name,
        "description": workflow.description,
        "inputs": dict(workflow.inputs),
        "nodes": [workflow_node_payload_dict(node) for node in workflow.nodes],
    }
    if workflow.worktrees:
        payload["worktrees"] = worktree_declarations_payload(workflow.worktrees)
    return payload


def validate_input_node_boundary(node: WorkflowNode, node_label: str) -> None:
    for rule in INPUT_NODE_CONTRACT_RULES:
        if rule.is_defined(node):
            raise ValueError(f"{node_label} must not define {rule.field_name}.")


def validate_input_node_contract(node: WorkflowNode, node_label: str) -> None:
    validate_input_node_boundary(node, node_label)

    source = node.source
    if source is None or not source.strip():
        raise ValueError(f"{node_label} requires a non-empty source.")
    if not INPUT_SOURCE_PATTERN.fullmatch(source.strip()):
        raise ValueError(
            f"{node_label} source must be exactly one raw {{{{file:...}}}} template."
        )


class WorkflowPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    name: str
    description: str = ""
    inputs: dict[str, str] = Field(default_factory=dict)
    worktrees: dict[str, WorktreeDeclaration] = Field(default_factory=dict)
    nodes: list[WorkflowNode]

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

    @model_validator(mode="after")
    def _validate_worktree_selectors(self) -> WorkflowPlan:
        for node in self.nodes:
            if (
                node.worktree is not None
                and node.worktree != PROJECT_ROOT_WORKTREE_SELECTOR
                and node.worktree not in self.worktrees
            ):
                raise ValueError(
                    f"Node '{node.id}' selects unknown worktree '{node.worktree}'"
                )
        return self


def render_prompt_for_role(node: WorkflowNode, role: PromptSegmentRole) -> str:
    return render_prompt_segments(node.prompt_segments, role)
