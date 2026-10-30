from __future__ import annotations

from datetime import datetime

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from crewplane.architecture.contracts import JsonObject
from crewplane.core.expansion_state import ExpansionState
from crewplane.version import SCHEMA_VERSION

from .compiled_records import (
    DependencyEdge,
    PreflightExecutionNode,
    RenderPlan,
    StaticResource,
    TokenCatalogEntry,
    WorkspaceFileLocator,
)
from .diagnostics import PreflightDiagnostic
from .plan_contract import (
    validate_current_execution_plan_shape,
    validate_execution_plan_semantics,
    validate_supported_plan_schema_version,
)
from .runtime_config import RuntimeConfigSnapshot
from .secrets import SecretContext
from .workspace import models as _workspace_models

WorkspaceBranchExportRecord = _workspace_models.WorkspaceBranchExportRecord
WorkspaceSelectionRecord = _workspace_models.WorkspaceSelectionRecord
WorkspaceSetupCommandRecord = _workspace_models.WorkspaceSetupCommandRecord
WorkspaceSetupRecord = _workspace_models.WorkspaceSetupRecord
WorkspaceSourceSnapshot = _workspace_models.WorkspaceSourceSnapshot

PREFLIGHT_STATUS_FAILED = "preflight_failed"
PREFLIGHT_STATUS_SUCCEEDED = "preflight_succeeded"


class CompiledForeach(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_id: str
    namespace: str
    workflow: str
    items: list[JsonObject] | str
    max_items: int = Field(strict=True, ge=0)
    item_node_id: str
    shared_bindings: dict[str, str]
    parameters: dict[str, str]
    original_dependencies: list[str]
    template_digest: str
    execution_order: list[str]
    nodes: list[PreflightExecutionNode]
    render_plans: list[RenderPlan]
    workspace_file_locators: list[WorkspaceFileLocator]
    token_catalog: list[TokenCatalogEntry]
    dependency_graph: list[DependencyEdge]

    def semantic_digest(self) -> str:
        from .plan_signatures import (
            semantic_node_payloads,
            semantic_source_location_payload,
        )
        from .signatures import signature_for_payload

        return signature_for_payload(
            {
                "nodes": semantic_node_payloads(self.nodes),
                "render_plans": semantic_source_location_payload(self.render_plans),
                "locators": semantic_source_location_payload(
                    self.workspace_file_locators
                ),
                "tokens": semantic_source_location_payload(self.token_catalog),
                "edges": self.dependency_graph,
                "item_node_id": self.item_node_id,
                "bindings": self.shared_bindings,
                "parameters": self.parameters,
            }
        )

    @property
    def node_bound(self) -> int:
        count = len(self.items) if isinstance(self.items, list) else self.max_items
        return count * len(self.nodes)


class PreflightCompilationPreview(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    plan_schema_version: str = SCHEMA_VERSION
    workflow_name: str | None = None
    workflow_signature: str | None = None
    execution_order: list[str] = Field(default_factory=list)
    foreach_templates: list[CompiledForeach] = Field(default_factory=list)
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
    secret_context: SecretContext = Field(
        default_factory=SecretContext,
        exclude=True,
        repr=False,
    )
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

    plan_schema_version: str
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
    expansions: dict[str, ExpansionState] = Field(default_factory=dict)
    foreach_templates: list[CompiledForeach] = Field(default_factory=list)
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
            self.nodes,
        )
        validate_execution_plan_semantics(self)
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
            plan_schema_version=preview.plan_schema_version,
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
            foreach_templates=list(preview.foreach_templates),
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
