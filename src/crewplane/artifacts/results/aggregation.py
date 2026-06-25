from __future__ import annotations

from pathlib import Path

from crewplane.architecture.ports.artifacts import StageTaskSpec

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
    task_ids = ordered_task_ids(selected_files, task_specs)
    section_titles = section_titles_by_task_id(task_ids, task_specs)
    for task_id in task_ids:
        add_output_to_aggregation(
            aggregation,
            task_id,
            section_titles[task_id],
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
    section_title: str,
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
    aggregation.result_sections.append(
        f"## {section_title}\n\n{display_content}\n\n---\n\n"
    )
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
            (section_title, build_failure_findings_content(task_id))
        )
        return
    if findings_selection.should_extract(task_id, raw_output):
        findings_content = extract_findings_content(raw_output, output_file)
        aggregation.findings_sections.append((section_title, findings_content))


def section_titles_by_task_id(
    task_ids: list[str],
    task_specs: tuple[StageTaskSpec, ...],
) -> dict[str, str]:
    if len(task_ids) == 1:
        return {task_ids[0]: "Output"}
    display_names = {
        task_spec.task_id: task_spec.display_name
        for task_spec in task_specs
        if task_spec.display_name
    }
    return {task_id: display_names.get(task_id, task_id) for task_id in task_ids}


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
