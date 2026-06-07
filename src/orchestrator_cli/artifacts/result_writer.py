from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from orchestrator_cli.architecture.ports.artifacts import (
    StageFinalizeResult,
    StageTaskSpec,
)

from .failure_artifacts import (
    is_synthetic_invocation_failure,
    strip_synthetic_invocation_failure_marker,
)
from .findings_extraction import (
    FindingsSelection,
    build_failure_findings_content,
    build_findings_document,
    extract_findings_content,
)
from .generated_files import GeneratedFileReferenceDetector
from .result_selection import is_raw_input_stage, latest_round_files, ordered_task_ids
from .review_loop_status import resolve_review_loop_status
from .stage_output_aggregation import StageOutputAggregation
from .stage_result_document import write_stage_result_file


class ResultWriter:
    """Write consolidated stage results."""

    def __init__(
        self,
        result_file_resolver: Callable[[str], Path],
        findings_file_resolver: Callable[[str], Path],
        empty_output_warning_enabled: bool,
        workspace_root: Path | None = None,
    ) -> None:
        self._result_file_resolver = result_file_resolver
        self._findings_file_resolver = findings_file_resolver
        self._empty_output_warning_enabled = empty_output_warning_enabled
        self._workspace_root = workspace_root.resolve() if workspace_root else None
        self._generated_file_detector = (
            GeneratedFileReferenceDetector(self._workspace_root)
            if self._workspace_root is not None
            else None
        )

    def finalize_stage(
        self,
        stage_name: str,
        stage_dir: Path | None,
        findings_enabled: bool = False,
        task_specs: tuple[StageTaskSpec, ...] = (),
    ) -> StageFinalizeResult:
        result_file = self._result_file_resolver(stage_name)
        findings_file = (
            self._findings_file_resolver(stage_name) if findings_enabled else None
        )
        if stage_dir is None:
            return StageFinalizeResult(
                stage_name=stage_name,
                result_file=result_file,
                findings_file=findings_file,
                included_outputs=(),
                skipped_empty_outputs=(),
                warnings=("Stage directory was not created before finalization.",),
            )

        selected_files = self._select_stage_files(stage_name, stage_dir)
        if is_raw_input_stage(selected_files):
            return self._write_raw_input_result(stage_name, result_file, selected_files)

        aggregation = self._aggregate_stage_outputs(
            selected_files,
            task_specs,
            findings_enabled,
        )
        write_stage_result_file(
            result_file,
            stage_name,
            aggregation.result_sections,
            aggregation.generated_file_reference_content,
            self._workspace_root,
            self._generated_file_detector,
        )
        resolved_findings_file = self._write_findings_file(
            findings_file,
            aggregation.findings_sections,
        )
        return StageFinalizeResult(
            stage_name=stage_name,
            result_file=result_file,
            findings_file=resolved_findings_file,
            included_outputs=tuple(aggregation.included_outputs),
            skipped_empty_outputs=tuple(aggregation.skipped_empty_outputs),
            warnings=tuple(self._warnings_for_skipped_outputs(aggregation)),
        )

    def _select_stage_files(self, stage_name: str, stage_dir: Path) -> dict[str, Path]:
        resolved_status = resolve_review_loop_status(stage_name, stage_dir)
        if resolved_status is not None:
            return resolved_status.selected_output_files
        return latest_round_files(stage_dir)

    def _aggregate_stage_outputs(
        self,
        selected_files: dict[str, Path],
        task_specs: tuple[StageTaskSpec, ...],
        findings_enabled: bool,
    ) -> StageOutputAggregation:
        aggregation = StageOutputAggregation()
        findings_selection = FindingsSelection.from_stage(
            task_specs,
            findings_enabled,
        )
        for task_id in ordered_task_ids(selected_files, task_specs):
            self._add_output_to_aggregation(
                aggregation,
                task_id,
                selected_files[task_id],
                findings_selection,
            )
        return aggregation

    def _add_output_to_aggregation(
        self,
        aggregation: StageOutputAggregation,
        task_id: str,
        output_file: Path,
        findings_selection: FindingsSelection,
    ) -> None:
        raw_output = output_file.read_text(encoding="utf-8")
        if not raw_output.strip():
            aggregation.skipped_empty_outputs.append(output_file)
            return

        display_content = strip_synthetic_invocation_failure_marker(raw_output).strip()
        aggregation.included_outputs.append(output_file)
        aggregation.result_sections.append(
            f"## {task_id}\n\n{display_content}\n\n---\n\n"
        )
        aggregation.generated_file_reference_content.append(display_content)
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

    def _write_raw_input_result(
        self,
        stage_name: str,
        result_file: Path,
        selected_files: dict[str, Path],
    ) -> StageFinalizeResult:
        input_file = selected_files["input"]
        result_file.parent.mkdir(parents=True, exist_ok=True)
        result_file.write_text(input_file.read_text(encoding="utf-8"), encoding="utf-8")
        return StageFinalizeResult(
            stage_name=stage_name,
            result_file=result_file,
            findings_file=None,
            included_outputs=(input_file,),
            skipped_empty_outputs=(),
            warnings=(),
        )

    def _write_findings_file(
        self,
        findings_file: Path | None,
        findings_sections: list[tuple[str, str]],
    ) -> Path | None:
        if findings_file is None:
            return None
        if not findings_sections:
            return None
        findings_file.parent.mkdir(parents=True, exist_ok=True)
        findings_file.write_text(
            build_findings_document(findings_sections),
            encoding="utf-8",
        )
        return findings_file

    def _warnings_for_skipped_outputs(
        self,
        aggregation: StageOutputAggregation,
    ) -> list[str]:
        if not self._empty_output_warning_enabled:
            return []
        return [
            f"Skipping empty output file {file_path.name}"
            for file_path in aggregation.skipped_empty_outputs
        ]
