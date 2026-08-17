from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from crewplane.architecture.ports.artifacts import (
    StageFinalizeResult,
    StageTaskSpec,
)
from crewplane.artifacts.atomic import atomic_write_text
from crewplane.core.workflow.keywords import ProviderRole

from ..generated_files.catalog import (
    GeneratedFileReferenceDetector,
)
from .aggregation import aggregate_stage_outputs
from .findings import (
    build_findings_document,
)
from .review_loop_status import ReviewLoopStatusError, resolve_review_loop_status
from .selection import is_raw_input_stage, latest_round_files
from .stage_document import write_stage_result_file
from .stage_outputs import StageOutputAggregation


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
        generated_file_detection_enabled: bool = True,
        generated_file_workspace_roots: dict[Path, Path | None] | None = None,
    ) -> StageFinalizeResult:
        result_file = self._result_file_resolver(stage_name)
        findings_file = (
            self._findings_file_resolver(stage_name) if findings_enabled else None
        )
        return self.finalize_at(
            stage_name,
            stage_dir,
            result_file,
            findings_file,
            findings_enabled=findings_enabled,
            task_specs=task_specs,
            generated_file_detection_enabled=generated_file_detection_enabled,
            generated_file_workspace_roots=generated_file_workspace_roots,
        )

    def finalize_at(
        self,
        stage_name: str,
        stage_dir: Path | None,
        result_file: Path,
        findings_file: Path | None,
        findings_enabled: bool = False,
        task_specs: tuple[StageTaskSpec, ...] = (),
        generated_file_detection_enabled: bool = True,
        generated_file_workspace_roots: dict[Path, Path | None] | None = None,
    ) -> StageFinalizeResult:
        """Finalize a stage at caller-supplied, compiled artifact locations."""

        if findings_enabled and findings_file is None:
            raise ValueError("Findings-enabled nodes require a findings locator.")
        if stage_dir is None:
            if any(spec.role == ProviderRole.REVIEWER for spec in task_specs):
                raise ReviewLoopStatusError(
                    "Invalid review-loop status artifact: required status is missing."
                )
            return StageFinalizeResult(
                stage_name=stage_name,
                result_file=result_file,
                findings_file=findings_file,
                included_outputs=(),
                skipped_empty_outputs=(),
                warnings=("Stage directory was not created before finalization.",),
            )

        selected_files = self._select_stage_files(stage_name, stage_dir, task_specs)
        if is_raw_input_stage(selected_files):
            if findings_enabled:
                raise ValueError("Input stages cannot enable findings output.")
            return self._write_raw_input_result(stage_name, result_file, selected_files)

        aggregation = aggregate_stage_outputs(
            selected_files,
            task_specs,
            findings_enabled,
            result_file,
            stage_name,
            generated_file_workspace_roots or {},
            generated_file_detection_enabled,
        )
        write_stage_result_file(
            result_file,
            stage_name,
            aggregation.result_sections,
            aggregation.generated_file_reference_content,
            aggregation.generated_file_links,
            self._generated_file_workspace_root(generated_file_detection_enabled),
            self._generated_file_detector_for(generated_file_detection_enabled),
        )
        resolved_findings_file = self._write_findings_file(
            findings_file,
            aggregation.findings_sections,
            required=findings_enabled,
        )
        return StageFinalizeResult(
            stage_name=stage_name,
            result_file=result_file,
            findings_file=resolved_findings_file,
            included_outputs=tuple(aggregation.included_outputs),
            skipped_empty_outputs=tuple(aggregation.skipped_empty_outputs),
            warnings=(
                *self._warnings_for_skipped_outputs(aggregation),
                *aggregation.warnings,
            ),
            generated_files=tuple(
                dict.fromkeys(
                    link.target_path for link in aggregation.generated_file_links
                )
            ),
        )

    def _select_stage_files(
        self,
        stage_name: str,
        stage_dir: Path,
        task_specs: tuple[StageTaskSpec, ...],
    ) -> dict[str, Path]:
        resolved_status = resolve_review_loop_status(
            stage_name,
            stage_dir,
            task_specs,
        )
        if resolved_status is not None:
            return resolved_status.selected_output_files
        if any(spec.role == ProviderRole.REVIEWER for spec in task_specs):
            raise ReviewLoopStatusError(
                "Invalid review-loop status artifact: required status is missing."
            )
        return latest_round_files(stage_dir)

    def _write_raw_input_result(
        self,
        stage_name: str,
        result_file: Path,
        selected_files: dict[str, Path],
    ) -> StageFinalizeResult:
        input_file = selected_files["input"]
        atomic_write_text(
            result_file,
            input_file.read_text(encoding="utf-8"),
        )
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
        required: bool = False,
    ) -> Path | None:
        if findings_file is None:
            return None
        if not findings_sections and not required:
            return None
        atomic_write_text(
            findings_file,
            build_findings_document(findings_sections),
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

    def _generated_file_workspace_root(self, enabled: bool) -> Path | None:
        if not enabled:
            return None
        return self._workspace_root

    def _generated_file_detector_for(
        self,
        enabled: bool,
    ) -> GeneratedFileReferenceDetector | None:
        if not enabled:
            return None
        return self._generated_file_detector
