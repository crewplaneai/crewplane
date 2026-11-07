from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from crewplane.core.workflow.composition.models import (
    ComposedForeach,
    WorkflowSourceRecord,
)
from crewplane.core.workflow.loading import load_tasks_with_sources
from crewplane.core.workflow.models import (
    WorkflowPayload,
    WorkflowPlan,
    workflow_payload_dict,
)
from crewplane.core.workflow.normalization import resolve_memory_default
from crewplane.core.workflow.source_locations import SourceSpan


@dataclass(frozen=True)
class PreflightWorkflowSource:
    """Workflow source data captured before preflight phase compilation."""

    workflow: WorkflowPlan
    workflow_content: str
    composed_workflow: WorkflowPayload
    referenced_workflows: list[WorkflowSourceRecord]
    node_source_paths: dict[str, Path]
    node_source_spans: dict[str, SourceSpan]
    prompt_segment_spans: dict[str, list[SourceSpan]]
    root_workflow_path: Path | None = None
    foreach_templates: dict[str, ComposedForeach] = field(default_factory=dict)

    @classmethod
    def from_workflow(
        cls,
        workflow: WorkflowPlan,
        workflow_content: str = "workflow source",
        composed_workflow: WorkflowPayload | None = None,
        referenced_workflows: list[WorkflowSourceRecord] | None = None,
        node_source_paths: dict[str, Path] | None = None,
        node_source_spans: dict[str, SourceSpan] | None = None,
        prompt_segment_spans: dict[str, list[SourceSpan]] | None = None,
        root_workflow_path: Path | None = None,
    ) -> PreflightWorkflowSource:
        workflow = _resolve_memory_defaults(workflow)
        return cls(
            workflow=workflow,
            workflow_content=workflow_content,
            composed_workflow=composed_workflow or workflow_payload_dict(workflow),
            referenced_workflows=list(referenced_workflows or []),
            node_source_paths=dict(node_source_paths or {}),
            node_source_spans=dict(node_source_spans or {}),
            prompt_segment_spans={
                node_id: list(spans)
                for node_id, spans in (prompt_segment_spans or {}).items()
            },
            root_workflow_path=root_workflow_path,
        )

    def referenced_workflow_payloads(self) -> list[dict[str, str]]:
        return [
            {"path": record.path.as_posix(), "sha256": record.sha256}
            for record in self.referenced_workflows
        ]


def load_workflow_source_for_preflight(
    tasks_file: Path,
    project_root: Path,
) -> PreflightWorkflowSource:
    """Parse and compose a workflow source without running reference validation."""

    loaded = load_tasks_with_sources(tasks_file, project_root=project_root)
    workflow = _resolve_memory_defaults(loaded.workflow)
    return PreflightWorkflowSource(
        workflow=workflow,
        workflow_content=loaded.workflow_content,
        composed_workflow=(
            loaded.composed_workflow
            if tasks_file.suffix.lower() == ".md"
            else workflow_payload_dict(workflow)
        ),
        referenced_workflows=loaded.referenced_workflows,
        node_source_paths=loaded.node_source_paths,
        node_source_spans=loaded.node_source_spans,
        prompt_segment_spans=loaded.prompt_segment_spans,
        root_workflow_path=tasks_file.resolve(strict=False),
        foreach_templates=loaded.foreach_templates,
    )


def _resolve_memory_defaults(workflow: WorkflowPlan) -> WorkflowPlan:
    default = resolve_memory_default(workflow.memory)
    nodes = [
        node.model_copy(update={"memory": resolve_memory_default(node.memory, default)})
        if node.mode not in {"input", "foreach"} and node.memory is None
        else node
        for node in workflow.nodes
    ]
    if all(
        resolved is original
        for resolved, original in zip(nodes, workflow.nodes, strict=True)
    ):
        return workflow
    return workflow.model_copy(update={"nodes": nodes})
