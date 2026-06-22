from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from orchestrator_cli.architecture.contracts import JsonObject
from orchestrator_cli.core.prompt_segments import PromptSegmentRole
from orchestrator_cli.core.workflow_keywords import NodeMode, ProviderRole
from orchestrator_cli.version import SCHEMA_VERSION

from .diagnostics import PreflightDiagnostic
from .plan_contract import (
    validate_current_execution_plan_shape,
    validate_supported_plan_schema_version,
)
from .runtime_config import RuntimeConfigSnapshot
from .secrets import SecretContext
from .workspace_models import (
    WorkspaceBranchExportRecord as WorkspaceBranchExportRecord,
)
from .workspace_models import (
    WorkspaceSelectionRecord as WorkspaceSelectionRecord,
)
from .workspace_models import (
    WorkspaceSetupCommandRecord as WorkspaceSetupCommandRecord,
)
from .workspace_models import (
    WorkspaceSetupRecord as WorkspaceSetupRecord,
)
from .workspace_models import (
    WorkspaceSourceSnapshot as WorkspaceSourceSnapshot,
)

PREFLIGHT_STATUS_FAILED = "preflight_failed"
PREFLIGHT_STATUS_SUCCEEDED = "preflight_succeeded"


TokenKind = Literal["node", "file", "env", "var"]
FragmentKind = Literal[
    "literal",
    "static_file_content",
    "workspace_file_locator",
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
        if (
            self.kind in {"runtime_locator_lookup", "workspace_file_locator"}
            and self.locator is None
        ):
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
    resolved: JsonObject = Field(default_factory=dict)
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


class TokenBudgetPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fail_threshold_chars: int | None = None
    warn_threshold_chars: int | None = None


class RetryPolicy(BaseModel):
    """Reserved for future compiled retry policy fields."""

    model_config = ConfigDict(extra="forbid")


class ConcurrencyPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_concurrent_nodes: int | None = None
    max_parallel_invocations: int | None = None


class ExecutionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    depth: int | None = None
    audit_rounds: int | None = None
    continue_on_failure: bool = False
    failure_threshold: int | None = None
    token_budget: TokenBudgetPolicy | None = None
    consensus_on_exhaustion: str | None = None
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    concurrency_policy: ConcurrencyPolicy = Field(default_factory=ConcurrencyPolicy)


class ArtifactContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage_path: str | None = None
    output_path: str
    findings_path: str | None = None
    log_path: str | None = None
    manifest_path: str | None = None
    result_path: str | None = None


WorkspaceFileSourceClass = Literal["project_initial", "runtime_dynamic"]
WorkspaceFileTarget = Literal["input_output", "executor_prompt", "reviewer_prompt"]


class WorkspaceFileLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locator_id: str
    content_ref: str | None = None
    occurrence_id: str
    node_id: str
    target: WorkspaceFileTarget
    source_class: WorkspaceFileSourceClass
    raw_token: str
    raw_path: str
    source_file: str | None = None
    source_span: dict[str, int] | None = None
    token_raw_span: dict[str, int] | None = None
    source_root: str
    source_root_relative_to_project: str
    project_root_relative_to_git_top: str
    git_top_relative_path: str
    workspace_relative_path: str
    runtime_dynamic_after_candidate: bool = False
    git_blob: str | None = None
    git_file_mode: str | None = None
    byte_size: int | None = None
    canonical_blob_sha256: str | None = None
    injected_sha256: str | None = None
    literal_path_verified: bool = False
    utf8_validated: bool = False


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
    workspace_policy: WorkspaceSelectionRecord | None = None
    artifact_contract: ArtifactContract
    input_content_ref: str | None = None
    input_workspace_file_locator_id: str | None = None


class PreflightCompilationPreview(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    plan_schema_version: str = SCHEMA_VERSION
    workflow_name: str | None = None
    workflow_signature: str | None = None
    execution_order: list[str] = Field(default_factory=list)
    nodes: list[PreflightExecutionNode] = Field(default_factory=list)
    render_plans: list[RenderPlan] = Field(default_factory=list)
    static_resources: list[StaticResource] = Field(default_factory=list)
    workspace_file_locators: list[WorkspaceFileLocator] = Field(default_factory=list)
    token_catalog: list[TokenCatalogEntry] = Field(default_factory=list)
    dependency_graph: list[DependencyEdge] = Field(default_factory=list)
    diagnostics: list[PreflightDiagnostic] = Field(default_factory=list)
    runtime_config_snapshot: RuntimeConfigSnapshot | None = None
    effective_runtime_config_signature: str | None = None
    workspace_source: WorkspaceSourceSnapshot | None = None
    value_fingerprints: list[dict[str, str]] = Field(default_factory=list)
    fingerprint_metadata: JsonObject = Field(default_factory=dict)
    secret_context: SecretContext = Field(default_factory=SecretContext, exclude=True)
    static_file_payloads: dict[str, bytes] = Field(default_factory=dict, exclude=True)
    workspace_file_payloads: dict[str, bytes] = Field(
        default_factory=dict,
        exclude=True,
    )

    @field_validator("plan_schema_version")
    @classmethod
    def _validate_plan_schema_version(cls, value: str) -> str:
        return validate_supported_plan_schema_version(value)

    def has_errors(self) -> bool:
        return any(diagnostic.severity == "error" for diagnostic in self.diagnostics)


class PreflightExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_schema_version: str = SCHEMA_VERSION
    run_id: str
    run_key_name: str
    project_root: str
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
    workspace_file_locators: list[WorkspaceFileLocator] = Field(default_factory=list)
    token_catalog: list[TokenCatalogEntry]
    dependency_graph: list[DependencyEdge]
    runtime_config_snapshot: JsonObject
    effective_runtime_config_signature: str
    workspace_source: WorkspaceSourceSnapshot | None = None
    value_fingerprints: list[dict[str, str]] = Field(default_factory=list)
    fingerprint_metadata: JsonObject

    @field_validator("plan_schema_version")
    @classmethod
    def _validate_plan_schema_version(cls, value: str) -> str:
        return validate_supported_plan_schema_version(value)

    @model_validator(mode="after")
    def _validate_current_plan_shape(self) -> PreflightExecutionPlan:
        validate_current_execution_plan_shape(
            self.runtime_config_snapshot,
            self.fingerprint_metadata,
            self.value_fingerprints,
        )
        return self

    @classmethod
    def from_preview(
        cls,
        preview: PreflightCompilationPreview,
        run_id: str,
        run_key_name: str,
        project_root: str,
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
            project_root=project_root,
            context_root=context_root,
            manifest_root=manifest_root,
            created_at=created_at.isoformat(),
            workflow_name=preview.workflow_name,
            workflow_signature=preview.workflow_signature,
            execution_order=list(preview.execution_order),
            nodes=list(preview.nodes),
            render_plans=list(preview.render_plans),
            static_resources=list(preview.static_resources),
            workspace_file_locators=list(preview.workspace_file_locators),
            token_catalog=list(preview.token_catalog),
            dependency_graph=list(preview.dependency_graph),
            runtime_config_snapshot=preview.runtime_config_snapshot.redacted_payload(),
            effective_runtime_config_signature=preview.effective_runtime_config_signature
            or "",
            workspace_source=preview.workspace_source,
            value_fingerprints=list(preview.value_fingerprints),
            fingerprint_metadata=dict(preview.fingerprint_metadata),
        )
