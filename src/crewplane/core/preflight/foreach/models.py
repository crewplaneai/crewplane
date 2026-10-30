from pydantic import BaseModel, ConfigDict

from crewplane.core.expansion_state import ExpansionState

from ..compiled_records import (
    DependencyEdge,
    PreflightExecutionNode,
    RenderPlan,
    TokenCatalogEntry,
    WorkspaceFileLocator,
)


class ExpansionBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: ExpansionState
    nodes: list[PreflightExecutionNode]
    render_plans: list[RenderPlan]
    workspace_file_locators: list[WorkspaceFileLocator]
    token_catalog: list[TokenCatalogEntry]
    dependency_graph: list[DependencyEdge]
    group_dependencies: list[str]
