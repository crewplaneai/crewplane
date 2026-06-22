from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from orchestrator_cli.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSourceSnapshot,
)

from .protected_refs import ProtectedRefSnapshot

WorkspaceSourceKind = Literal["project", "node", "candidate"]


@dataclass(frozen=True)
class WorktreeSourceRef:
    source_kind: WorkspaceSourceKind
    source_node_id: str | None
    source_commit: str
    source_tree: str
    candidate_sequence: int | None = None
    bundle_path: Path | None = None
    bundle_sha256: str | None = None
    bundle_size_bytes: int | None = None
    bundle_ref: str | None = None
    upstream_sources: tuple[WorktreeSourceRef, ...] = ()


@dataclass(frozen=True)
class WorktreeWorkspace:
    workspace_path: Path
    checkout_root: Path
    cwd: Path
    git_dir: Path
    source_ref: WorktreeSourceRef
    protected_refs: ProtectedRefSnapshot
    lock_mode: str


@dataclass(frozen=True)
class WorktreeCaptureRequest:
    plan: PreflightExecutionPlan
    source: WorkspaceSourceSnapshot
    source_ref: WorktreeSourceRef
    workspace_path: Path
    checkout_root: Path
    git_dir: Path
    node_id: str
    task_id: str
    state_path: Path
    slug: str
    protected_refs: ProtectedRefSnapshot


@dataclass(frozen=True)
class WorktreeCaptureResult:
    candidate_commit: str
    result_commit: str
    candidate_tree: str
    result_tree: str
    changed_path_count: int
    bundle_path: Path
    bundle_sha256: str
    bundle_size_bytes: int
    candidate_ref: str
    result_ref: str
    final_head: str
