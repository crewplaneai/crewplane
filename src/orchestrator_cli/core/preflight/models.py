from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from orchestrator_cli.core.prompt_segments import PromptSegmentRole
from orchestrator_cli.core.versions import PREFLIGHT_PLAN_SCHEMA_VERSION
from orchestrator_cli.core.workflow_keywords import NodeMode, ProviderRole

from .diagnostics import PreflightDiagnostic
from .runtime_config import RuntimeConfigSnapshot
from .secrets import SecretContext

PREFLIGHT_STATUS_FAILED = "preflight_failed"
PREFLIGHT_STATUS_SUCCEEDED = "preflight_succeeded"

TokenKind = Literal["node", "file", "env", "var"]
FragmentKind = Literal[
    "literal",
    "static_file_content",
    "static_env",
    "static_var",
    "runtime_locator_lookup",
]


class StaticResource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_id: str
    kind: Literal["file"]
    raw_path: str
    source_root: str
    resolved_path: str
    content_ref: str
    encoding: str = "utf-8"
    size_bytes: int
    sha256: str
    token_signatures: list[str] = Field(default_factory=list)


class Fragment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fragment_index: int
    kind: FragmentKind
    source_role: PromptSegmentRole
    text: str | None = None
    content_ref: str | None = None
    key: str | None = None
    value_handle: str | None = None
    value_stored: str | None = None
    fingerprint: str | None = None
    locator: dict[str, str] | None = None

    @model_validator(mode="after")
    def _validate_fragment_payload(self) -> Fragment:
        if self.kind == "literal" and self.text is None:
            raise ValueError("Literal fragments require text.")
        if self.kind == "static_file_content" and self.content_ref is None:
            raise ValueError("Static file fragments require content_ref.")
        if self.kind == "runtime_locator_lookup" and self.locator is None:
            raise ValueError("Runtime locator fragments require locator.")
        if self.kind in {"static_env", "static_var"}:
            if self.key is None:
                raise ValueError("Static value fragments require key.")
            if self.value_handle is None and self.value_stored is None:
                raise ValueError(
                    "Static value fragments require a handle or stored value."
                )
        return self


class RenderStream(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_role: ProviderRole
    fragments: list[Fragment] = Field(default_factory=list)


class RenderPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    render_plan_id: str
    node_id: str | None = None
    module_id: str | None = None
    source_file: str | None = None
    source_root: str | None = None
    source_span: dict[str, int] | None = None
    template_hash: str | None = None
    template_signature: str | None = None
    streams: list[RenderStream] = Field(default_factory=list)


class TokenCatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    occurrence_id: str
    node_id: str
    target_role: ProviderRole
    source_role: PromptSegmentRole
    raw_token: str
    token_kind: TokenKind
    fragment_index: int
    signature: str
    source_file: str | None = None
    source_span: dict[str, int] | None = None
    token_raw_span: dict[str, int] | None = None
    canonical_locator: str | None = None
    dependency_signature: str | None = None
    resolved: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("token_kind")
    @classmethod
    def _reject_param_token_kind(cls, value: TokenKind) -> TokenKind:
        if value == "param":
            raise ValueError(
                "Param tokens are composition-only and cannot be persisted."
            )
        return value


class DependencyEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_node: str
    target_node: str
    artifact_name: str | None = None
    dependency_signature: str
    target_locator: str | None = None
    artifact_key: str | None = None
    first_token_signature: str | None = None


class ProviderRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    role: ProviderRole
    model: str | None = None
    task_id: str
    agent_config_key: str
    invoker_alias: str
    agent_config_signature: str
    invoker_config_signature: str


class ExecutionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    depth: int | None = None
    audit_rounds: int | None = None
    continue_on_failure: bool = False
    failure_threshold: int | None = None
    token_budget: dict[str, Any] | None = None
    consensus_on_exhaustion: str | None = None
    retry_policy: dict[str, Any] = Field(default_factory=dict)
    concurrency_policy: dict[str, Any] = Field(default_factory=dict)


class ArtifactContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage_path: str | None = None
    output_path: str
    findings_path: str | None = None
    log_path: str | None = None
    manifest_path: str | None = None
    result_path: str | None = None


class PreflightExecutionNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    module_id: str | None = None
    source_file: str | None = None
    source_root: str | None = None
    source_span: dict[str, int] | None = None
    mode: NodeMode
    findings: bool = False
    dependencies: list[str] = Field(default_factory=list)
    render_plan_id: str | None = None
    provider_records: list[ProviderRecord] = Field(default_factory=list)
    execution_policy: ExecutionPolicy = Field(default_factory=ExecutionPolicy)
    artifact_contract: ArtifactContract
    input_content_ref: str | None = None


class PreflightCompilationPreview(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    plan_schema_version: str = PREFLIGHT_PLAN_SCHEMA_VERSION
    workflow_name: str | None = None
    workflow_signature: str | None = None
    execution_order: list[str] = Field(default_factory=list)
    nodes: list[PreflightExecutionNode] = Field(default_factory=list)
    render_plans: list[RenderPlan] = Field(default_factory=list)
    static_resources: list[StaticResource] = Field(default_factory=list)
    token_catalog: list[TokenCatalogEntry] = Field(default_factory=list)
    dependency_graph: list[DependencyEdge] = Field(default_factory=list)
    diagnostics: list[PreflightDiagnostic] = Field(default_factory=list)
    runtime_config_snapshot: RuntimeConfigSnapshot | None = None
    effective_runtime_config_signature: str | None = None
    value_fingerprints: list[dict[str, str]] = Field(default_factory=list)
    fingerprint_metadata: dict[str, Any] = Field(default_factory=dict)
    secret_context: SecretContext = Field(default_factory=SecretContext, exclude=True)
    static_file_payloads: dict[str, bytes] = Field(default_factory=dict, exclude=True)

    def has_errors(self) -> bool:
        return any(diagnostic.severity == "error" for diagnostic in self.diagnostics)


class PreflightExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_schema_version: str = PREFLIGHT_PLAN_SCHEMA_VERSION
    run_id: str
    run_key_name: str
    context_root: str
    manifest_root: str
    created_at: str
    completed_at: str | None = None
    workflow_name: str
    workflow_signature: str
    execution_order: list[str]
    nodes: list[PreflightExecutionNode]
    render_plans: list[RenderPlan]
    static_resources: list[StaticResource]
    token_catalog: list[TokenCatalogEntry]
    dependency_graph: list[DependencyEdge]
    runtime_config_snapshot: dict[str, Any]
    effective_runtime_config_signature: str
    value_fingerprints: list[dict[str, str]] = Field(default_factory=list)
    fingerprint_metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_preview(
        cls,
        preview: PreflightCompilationPreview,
        run_id: str,
        run_key_name: str,
        context_root: str,
        manifest_root: str,
        created_at: datetime,
    ) -> PreflightExecutionPlan:
        if preview.workflow_name is None or preview.workflow_signature is None:
            raise ValueError("Successful preview requires workflow identity.")
        if preview.runtime_config_snapshot is None:
            raise ValueError("Successful preview requires runtime config snapshot.")
        return cls(
            run_id=run_id,
            run_key_name=run_key_name,
            context_root=context_root,
            manifest_root=manifest_root,
            created_at=created_at.isoformat(),
            workflow_name=preview.workflow_name,
            workflow_signature=preview.workflow_signature,
            execution_order=list(preview.execution_order),
            nodes=list(preview.nodes),
            render_plans=list(preview.render_plans),
            static_resources=list(preview.static_resources),
            token_catalog=list(preview.token_catalog),
            dependency_graph=list(preview.dependency_graph),
            runtime_config_snapshot=preview.runtime_config_snapshot.redacted_payload(),
            effective_runtime_config_signature=preview.effective_runtime_config_signature
            or "",
            value_fingerprints=list(preview.value_fingerprints),
            fingerprint_metadata=dict(preview.fingerprint_metadata),
        )
