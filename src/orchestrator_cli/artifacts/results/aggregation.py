from __future__ import annotations

from pathlib import Path

from orchestrator_cli.architecture.ports.artifacts import StageTaskSpec

from ..failure_artifacts import (
    is_synthetic_invocation_failure,
    strip_synthetic_invocation_failure_marker,
)
from ..generated_files.catalog import generated_file_links_for_content
from .findings import (
    FindingsSelection,
    build_failure_findings_content,
    extract_findings_content,
)
from .selection import ordered_task_ids
from .stage_outputs import StageOutputAggregation


def aggregate_stage_outputs(
    selected_files: dict[str, Path],
    task_specs: tuple[StageTaskSpec, ...],
    findings_enabled: bool,
    result_file: Path,
    stage_name: str,
    generated_file_workspace_roots: dict[Path, Path],
    generated_file_detection_enabled: bool,
) -> StageOutputAggregation:
    aggregation = StageOutputAggregation()
    findings_selection = FindingsSelection.from_stage(
        task_specs,
        findings_enabled,
    )
    for task_id in ordered_task_ids(selected_files, task_specs):
        add_output_to_aggregation(
            aggregation,
            task_id,
            selected_files[task_id],
            findings_selection,
            result_file,
            stage_name,
            generated_file_workspace_roots,
            generated_file_detection_enabled,
        )
    return aggregation


def add_output_to_aggregation(
    aggregation: StageOutputAggregation,
    task_id: str,
    output_file: Path,
    findings_selection: FindingsSelection,
    result_file: Path,
    stage_name: str,
    generated_file_workspace_roots: dict[Path, Path],
    generated_file_detection_enabled: bool,
) -> None:
    raw_output = output_file.read_text(encoding="utf-8")
    if not raw_output.strip():
        aggregation.skipped_empty_outputs.append(output_file)
        return

    display_content = strip_synthetic_invocation_failure_marker(raw_output).strip()
    aggregation.included_outputs.append(output_file)
    aggregation.result_sections.append(f"## {task_id}\n\n{display_content}\n\n---\n\n")
    record_generated_file_links(
        aggregation,
        task_id,
        display_content,
        output_file,
        result_file,
        stage_name,
        generated_file_workspace_roots,
        generated_file_detection_enabled,
    )
    if findings_selection.selects_task(task_id) and is_synthetic_invocation_failure(
        raw_output
    ):
        aggregation.findings_sections.append(
            (task_id, build_failure_findings_content(task_id))
        )
        return
    if findings_selection.should_extract(task_id, raw_output):
        findings_content = extract_findings_content(raw_output, output_file)
        aggregation.findings_sections.append((task_id, findings_content))


def record_generated_file_links(
    aggregation: StageOutputAggregation,
    task_id: str,
    display_content: str,
    output_file: Path,
    result_file: Path,
    stage_name: str,
    generated_file_workspace_roots: dict[Path, Path],
    detection_enabled: bool,
) -> None:
    if not detection_enabled:
        return
    workspace_root = generated_file_workspace_roots.get(
        output_file.resolve(strict=False)
    )
    if workspace_root is None:
        aggregation.generated_file_reference_content.append(display_content)
        return
    aggregation.generated_file_links.extend(
        generated_file_links_for_content(
            display_content,
            workspace_root,
            result_file,
            stage_name,
            materialize=True,
            copy_namespace=task_id,
        )
    )
