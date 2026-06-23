import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from yaml.constructor import ConstructorError

from crewplane.core.yaml_loader import load_yaml_unique

from .composition import compose_workflow_markdown
from .composition.models import (
    ComposedWorkflowDocument,
    WorkflowSourceRecord,
)
from .models import WorkflowPayload, WorkflowPlan, workflow_payload_dict


@dataclass(frozen=True)
class WorkflowLoadResult:
    workflow: WorkflowPlan
    workflow_content: str
    composed_workflow: WorkflowPayload
    referenced_workflows: list[WorkflowSourceRecord]
    node_source_paths: dict[str, Path]
    node_source_spans: dict[str, dict[str, int]]
    prompt_segment_spans: dict[str, list[dict[str, int]]]


def _build_workflow_load_result(
    path: Path,
    data: object,
    workflow_content: str,
    referenced_workflows: list[WorkflowSourceRecord],
    node_source_paths: dict[str, Path],
    node_source_spans: dict[str, dict[str, int]],
    prompt_segment_spans: dict[str, list[dict[str, int]]],
) -> WorkflowLoadResult:
    if not isinstance(data, dict):
        raise ValueError("Workflow file must contain a YAML object.")
    workflow_data = dict(data)
    if path.suffix.lower() == ".md":
        workflow_data.pop("imports", None)
    workflow = WorkflowPlan.model_validate(workflow_data)
    composed_workflow = (
        cast(WorkflowPayload, data)
        if path.suffix.lower() == ".md"
        else workflow_payload_dict(workflow)
    )
    return WorkflowLoadResult(
        workflow=workflow,
        workflow_content=workflow_content,
        composed_workflow=composed_workflow,
        referenced_workflows=referenced_workflows,
        node_source_paths=node_source_paths,
        node_source_spans=node_source_spans,
        prompt_segment_spans=prompt_segment_spans,
    )


def _raw_workflow_source_record(
    path: Path, workflow_content: str
) -> WorkflowSourceRecord:
    return WorkflowSourceRecord(
        path=path.resolve(strict=False),
        sha256=hashlib.sha256(workflow_content.encode("utf-8")).hexdigest(),
    )


def load_tasks_with_sources(
    path: Path,
    project_root: Path | None = None,
) -> WorkflowLoadResult:
    """Load a workflow file and retain the composed source provenance records."""

    with path.open("r", encoding="utf-8", newline="") as handle:
        workflow_content = handle.read()
    data: object
    if path.suffix.lower() == ".md":
        composed: ComposedWorkflowDocument = compose_workflow_markdown(
            path=path,
            project_root=project_root,
        )
        data = composed.workflow_payload
        referenced_workflows = composed.source_records
        node_source_paths = composed.node_source_paths
        node_source_spans = composed.node_source_spans
        prompt_segment_spans = composed.prompt_segment_spans
    else:
        try:
            data = load_yaml_unique(workflow_content)
        except ConstructorError as error:
            raise ValueError(f"{path} is invalid: {error}") from error
        referenced_workflows = [_raw_workflow_source_record(path, workflow_content)]
        node_source_paths = {}
        node_source_spans = {}
        prompt_segment_spans = {}

    return _build_workflow_load_result(
        path=path,
        data=data,
        workflow_content=workflow_content,
        referenced_workflows=referenced_workflows,
        node_source_paths=node_source_paths,
        node_source_spans=node_source_spans,
        prompt_segment_spans=prompt_segment_spans,
    )


def load_tasks(path: Path) -> WorkflowPlan:
    """Load a workflow file into a workflow plan."""

    return load_tasks_with_sources(path).workflow
