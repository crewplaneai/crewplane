from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from crewplane.core.workspace.policy import WorktreeDeclaration

from ..models import WorkflowNode, WorkflowPayload


@dataclass(frozen=True)
class WorkflowSourceRecord:
    path: Path
    sha256: str


@dataclass(frozen=True)
class ComposedWorkflowDocument:
    workflow_payload: WorkflowPayload
    source_records: list[WorkflowSourceRecord]
    node_source_paths: dict[str, Path]
    node_source_spans: dict[str, dict[str, int]]
    prompt_segment_spans: dict[str, list[dict[str, int]]]


@dataclass(frozen=True)
class ParsedWorkflow:
    path: Path
    schema_version: str
    name: str
    description: str
    inputs: dict[str, str]
    worktrees: dict[str, WorktreeDeclaration]
    imports: tuple[ImportSpec, ...]
    nodes: tuple[NodeSpec, ...]


@dataclass(frozen=True)
class ImportSpec:
    alias: str
    raw_path: str
    with_params: dict[str, str]
    inputs: dict[str, str]
    source_path: Path


@dataclass(frozen=True)
class NodeSpec:
    payload: WorkflowNode
    source_path: Path
    source_span: dict[str, int] | None
    prompt_segment_spans: tuple[dict[str, int], ...]


@dataclass(frozen=True)
class ComposedNode:
    payload: WorkflowNode
    source_path: Path
    source_span: dict[str, int] | None
    prompt_segment_spans: tuple[dict[str, int], ...]
    local_worktree_count: int = 0
    implicit_worktree_selector: str | None = None


@dataclass(frozen=True)
class ParamBinding:
    value: str
    binding_id: str


@dataclass(frozen=True)
class CompositionContext:
    workflow: ParsedWorkflow
    namespace_prefix: str
    inherited_params: dict[str, ParamBinding]
    bound_input_nodes: dict[str, str]
    import_stack: tuple[Path, ...]
    implicit_worktree_selector: str | None = None


@dataclass(frozen=True)
class CompositionResult:
    nodes: list[ComposedNode]
    worktrees: dict[str, WorktreeDeclaration]
    consumed_param_bindings: set[str]
