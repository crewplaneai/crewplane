from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import NotRequired, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator

from orchestrator_cli.versions import WORKFLOW_SCHEMA_VERSION

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

    @field_validator("mode", mode="before")
    @classmethod
    def _validate_mode(cls, value: object) -> object:
        return validate_exact_keyword(
            value,
            field_name="node mode",
            allowed_values=ALLOWED_NODE_MODES,
            allowed_value_set=ALLOWED_NODE_MODE_SET,
        )


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
    return node_payload


def workflow_payload_dict(workflow: WorkflowPlan) -> WorkflowPayload:
    return {
        "schema_version": workflow.schema_version,
        "name": workflow.name,
        "description": workflow.description,
        "inputs": dict(workflow.inputs),
        "nodes": [workflow_node_payload_dict(node) for node in workflow.nodes],
    }


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

    schema_version: str = WORKFLOW_SCHEMA_VERSION
    name: str
    description: str = ""
    inputs: dict[str, str] = Field(default_factory=dict)
    nodes: list[WorkflowNode]

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: str) -> str:
        if value != WORKFLOW_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported workflow schema version '{value}'. "
                f"Expected '{WORKFLOW_SCHEMA_VERSION}'."
            )
        return value


def render_prompt_for_role(node: WorkflowNode, role: PromptSegmentRole) -> str:
    return render_prompt_segments(node.prompt_segments, role)
