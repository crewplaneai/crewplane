from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from crewplane.architecture.ports import ArtifactStorePort
from crewplane.core.preflight.models import (
    PreflightExecutionNode,
    PreflightExecutionPlan,
    WorkspaceSelectionRecord,
    WorkspaceSourceSnapshot,
)
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.workspace.materialization import MaterializationLimiter
from crewplane.runtime.workspace.setup import WorkspaceSetupCancellation
from crewplane.runtime.workspace.state import RenderedWorkspaceFileDescriptor
from crewplane.runtime.workspace.worktree import WorktreeCaptureRequest
from crewplane.runtime.workspace.worktree.cache import WorktreeReuseCache
from crewplane.runtime.workspace.worktree.materialization import (
    WorktreeMaterialization,
)
from crewplane.runtime.workspace.worktree.types import WorktreeSourceRef


@dataclass(frozen=True)
class WorkspaceInvocationRequest:
    plan: PreflightExecutionPlan
    output: ArtifactStorePort
    node_id: str
    task_id: str
    provider: str
    role_label: ProviderRole
    round_num: int
    audit_round_num: int | None
    materialization_limiter: MaterializationLimiter | None = None
    worktree_reuse_cache: WorktreeReuseCache | None = None
    rendered_workspace_files: tuple[RenderedWorkspaceFileDescriptor, ...] = ()
    setup_cancellation: WorkspaceSetupCancellation | None = None


@dataclass(frozen=True)
class SnapshotPreparationPlan:
    node: PreflightExecutionNode
    policy: WorkspaceSelectionRecord
    source: WorkspaceSourceSnapshot
    slug: str
    state_path: Path
    child_environment_required: bool
    planned_workspace_path: Path


@dataclass(frozen=True)
class MaterializedSnapshotWorkspace:
    workspace_path: Path
    checkout_root: Path
    cwd: Path
    initial_snapshot_entries: dict[str, str]
    provisioning_duration_seconds: float
    checkout_size_bytes: int


@dataclass(frozen=True)
class WorktreePreparationPlan:
    node: PreflightExecutionNode
    policy: WorkspaceSelectionRecord
    source: WorkspaceSourceSnapshot
    source_ref: WorktreeSourceRef
    slug: str
    state_path: Path
    child_environment_required: bool
    lineage_producer: bool
    protected_ref_scopes: tuple[str, ...]
    planned_workspace_path: Path


@dataclass(frozen=True)
class MaterializedWorktreeWorkspace:
    materialized: WorktreeMaterialization
    capture_request: WorktreeCaptureRequest
    provisioning_duration_seconds: float
    checkout_size_bytes: int
