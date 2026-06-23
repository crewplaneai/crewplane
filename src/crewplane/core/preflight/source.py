from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from yaml.constructor import ConstructorError

from crewplane.core.workflow.composition import compose_workflow_markdown
from crewplane.core.workflow.composition.models import (
    WorkflowSourceRecord,
)
from crewplane.core.workflow.models import (
    WorkflowPayload,
    WorkflowPlan,
    workflow_payload_dict,
)
from crewplane.core.yaml_loader import load_yaml_unique


@dataclass(frozen=True)
class PreflightWorkflowSource:
    """Workflow source data captured before preflight phase compilation."""

    workflow: WorkflowPlan
    workflow_content: str
    composed_workflow: WorkflowPayload
    referenced_workflows: list[WorkflowSourceRecord]
    node_source_paths: dict[str, Path]
    node_source_spans: dict[str, dict[str, int]]
    prompt_segment_spans: dict[str, list[dict[str, int]]]
    root_workflow_path: Path | None = None

    @classmethod
    def from_workflow(
        cls,
        workflow: WorkflowPlan,
        workflow_content: str = "workflow source",
        composed_workflow: WorkflowPayload | None = None,
        referenced_workflows: list[WorkflowSourceRecord] | None = None,
        node_source_paths: dict[str, Path] | None = None,
        node_source_spans: dict[str, dict[str, int]] | None = None,
        prompt_segment_spans: dict[str, list[dict[str, int]]] | None = None,
        root_workflow_path: Path | None = None,
    ) -> PreflightWorkflowSource:
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

    with tasks_file.open("r", encoding="utf-8", newline="") as handle:
        workflow_content = handle.read()
    if tasks_file.suffix.lower() == ".md":
        return _load_markdown_workflow_source(
            tasks_file,
            project_root,
            workflow_content,
        )
    return _load_yaml_workflow_source(tasks_file, workflow_content)


def _load_markdown_workflow_source(
    tasks_file: Path,
    project_root: Path,
    workflow_content: str,
) -> PreflightWorkflowSource:
    composed = compose_workflow_markdown(path=tasks_file, project_root=project_root)
    return _build_workflow_source(
        tasks_file,
        composed.workflow_payload,
        workflow_content,
        composed.source_records,
        composed.node_source_paths,
        composed.node_source_spans,
        composed.prompt_segment_spans,
    )


def _load_yaml_workflow_source(
    tasks_file: Path,
    workflow_content: str,
) -> PreflightWorkflowSource:
    try:
        data = load_yaml_unique(workflow_content)
    except ConstructorError as error:
        raise ValueError(f"{tasks_file} is invalid: {error}") from error
    referenced_workflows = [
        WorkflowSourceRecord(
            path=tasks_file.resolve(strict=False),
            sha256=hashlib.sha256(workflow_content.encode("utf-8")).hexdigest(),
        )
    ]
    return _build_workflow_source(
        tasks_file,
        data,
        workflow_content,
        referenced_workflows,
        {},
        {},
        {},
    )


def _build_workflow_source(
    tasks_file: Path,
    data: object,
    workflow_content: str,
    referenced_workflows: list[WorkflowSourceRecord],
    node_source_paths: dict[str, Path],
    node_source_spans: dict[str, dict[str, int]],
    prompt_segment_spans: dict[str, list[dict[str, int]]],
) -> PreflightWorkflowSource:
    if not isinstance(data, dict):
        raise ValueError("Workflow file must contain a YAML object.")
    workflow_payload = dict(data)
    if tasks_file.suffix.lower() == ".md":
        workflow_payload.pop("imports", None)
    workflow = WorkflowPlan.model_validate(workflow_payload)
    composed_workflow = (
        cast(WorkflowPayload, data)
        if tasks_file.suffix.lower() == ".md"
        else workflow_payload_dict(workflow)
    )
    return PreflightWorkflowSource(
        workflow=workflow,
        workflow_content=workflow_content,
        composed_workflow=composed_workflow,
        referenced_workflows=referenced_workflows,
        node_source_paths=node_source_paths,
        node_source_spans=node_source_spans,
        prompt_segment_spans=prompt_segment_spans,
        root_workflow_path=tasks_file.resolve(strict=False),
    )
