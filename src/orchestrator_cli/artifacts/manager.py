from __future__ import annotations

import json
import re
from pathlib import Path

from orchestrator_cli.architecture.contracts import JsonObject
from orchestrator_cli.architecture.ports.artifacts import (
    StageFinalizeResult,
    StageTaskSpec,
)
from orchestrator_cli.core.preflight.models import PreflightExecutionPlan
from orchestrator_cli.core.preflight.serialization import pretty_sorted_json

from .directory_manager import DirectoryManager, safe_artifact_name
from .result_writer import ResultWriter

WORKFLOW_SIGNATURE_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _validate_workflow_signature(workflow_signature: str) -> str:
    if not WORKFLOW_SIGNATURE_PATTERN.fullmatch(workflow_signature):
        raise ValueError(
            "Manifest workflow_signature must be 64 lowercase hexadecimal characters."
        )
    return workflow_signature


def _iter_run_dirs(base_dir: Path, task_name: str) -> list[Path]:
    stages_root = base_dir / "execution-stages"
    safe_task_name = safe_artifact_name(task_name)
    if not stages_root.exists():
        return []
    run_dir_name_pattern = re.compile(
        rf"^{re.escape(safe_task_name)}-\d{{8}}-\d{{6}}(?:-\d{{6}})?$"
    )
    return sorted(
        (
            candidate
            for candidate in stages_root.iterdir()
            if run_dir_name_pattern.fullmatch(candidate.name)
            if candidate.is_dir()
        ),
        key=lambda candidate: candidate.name,
        reverse=True,
    )


def _read_manifest_result(manifest_file: Path) -> bool | None:
    try:
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    status = data.get("status")
    if status == "succeeded":
        return True
    if status == "failed":
        return False
    return None


def filesystem_manifest_exists(
    base_dir: Path,
    task_name: str,
    workflow_signature: str,
) -> bool:
    safe_workflow_signature = _validate_workflow_signature(workflow_signature)
    for run_dir in _iter_run_dirs(base_dir.resolve(), task_name):
        manifest_file = run_dir / "manifests" / f"{safe_workflow_signature}.json"
        if not manifest_file.exists():
            continue
        manifest_result = _read_manifest_result(manifest_file)
        if manifest_result is True:
            return True
    return False


class OutputManager:
    """Manage stage outputs, manifests, consolidated results, and preflight artifacts."""

    def __init__(
        self,
        task_name: str,
        base_dir: Path = Path("."),
        template_base_dir: Path | None = None,
        log_cli_output: bool = False,
    ) -> None:
        resolved_template_base_dir = (
            template_base_dir.resolve()
            if template_base_dir is not None
            else base_dir.resolve()
        )
        self._directories = DirectoryManager(
            task_name=task_name,
            base_dir=base_dir,
            log_cli_output=log_cli_output,
        )
        self._result_writer = ResultWriter(
            result_file_resolver=self._directories.get_stage_result_file,
            findings_file_resolver=self._directories.get_stage_findings_file,
            empty_output_warning_enabled=True,
            workspace_root=resolved_template_base_dir,
        )

    @staticmethod
    def _safe_name(name: str) -> str:
        return safe_artifact_name(name)

    @property
    def base_dir(self) -> Path:
        return self._directories.base_dir

    @property
    def task_name(self) -> str:
        return self._directories.task_name

    @property
    def log_cli_output(self) -> bool:
        return self._directories.log_cli_output

    @property
    def run_id(self) -> str:
        return self._directories.run_id

    @property
    def stages_dir(self) -> Path:
        return self._directories.stages_dir

    @property
    def results_dir(self) -> Path:
        return self._directories.results_dir

    @property
    def logs_dir(self) -> Path:
        return self._directories.logs_dir

    def create_stage_dir(self, stage_name: str) -> Path:
        return self._directories.create_stage_dir(stage_name)

    def get_stage_dir(self, stage_name: str) -> Path | None:
        return self._directories.get_stage_dir(stage_name)

    def finalize_stage(
        self,
        stage_name: str,
        findings_enabled: bool = False,
        task_specs: tuple[StageTaskSpec, ...] = (),
    ) -> StageFinalizeResult:
        return self._result_writer.finalize_stage(
            stage_name,
            self.get_stage_dir(stage_name),
            findings_enabled=findings_enabled,
            task_specs=task_specs,
        )

    def get_run_log_dir(self) -> Path:
        return self._directories.ensure_run_logs_dir()

    def get_orchestrator_event_log_path(self) -> Path:
        return self._directories.get_orchestrator_event_log_path()

    def get_orchestrator_summary_path(self) -> Path:
        return self._directories.get_orchestrator_summary_path()

    def get_log_file(
        self,
        stage_name: str,
        provider: str,
        task_id: str,
        audit_round_num: int | None = None,
        round_num: int | None = None,
    ) -> Path | None:
        return self._directories.get_log_file(
            stage_name,
            provider,
            task_id,
            audit_round_num,
            round_num,
        )

    def get_stage_output_path(self, stage_name: str) -> Path:
        return self._directories.get_stage_result_file(stage_name)

    def get_stage_findings_path(self, stage_name: str) -> Path:
        return self._directories.get_stage_findings_file(stage_name)

    def write_preflight_plan(self, plan: PreflightExecutionPlan) -> Path:
        preflight_dir = self.stages_dir / "preflight"
        preflight_dir.mkdir(parents=True, exist_ok=True)
        plan_path = preflight_dir / "execution-plan.json"
        plan_path.write_text(pretty_sorted_json(plan) + "\n", encoding="utf-8")
        return plan_path

    def write_preflight_static_file(self, content_ref: str, payload: bytes) -> Path:
        normalized_ref = Path(content_ref)
        if normalized_ref.is_absolute() or ".." in normalized_ref.parts:
            raise ValueError(f"Invalid preflight content reference '{content_ref}'.")
        path = self.stages_dir / "preflight" / normalized_ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def write_preflight_manifest(self, payload: object) -> Path:
        return self.write_preflight_json("manifest.json", payload)

    def write_preflight_diagnostics(self, payload: object) -> Path:
        return self.write_preflight_json("diagnostics.json", payload)

    def write_preflight_metadata(self, payload: object) -> Path:
        return self.write_preflight_json("metadata.json", payload)

    def write_preflight_summary(self, content: str) -> Path:
        return self.write_preflight_text("summary.md", content)

    def write_preflight_render_plan(self, payload: object) -> Path:
        return self.write_preflight_json("render-plans.json", payload)

    def write_preflight_execution_bundle(self, payload: object) -> Path:
        return self.write_preflight_json("execution-bundle.json", payload)

    def write_preflight_json(self, relative_path: str, payload: object) -> Path:
        path = self._preflight_artifact_path(relative_path)
        path.write_text(pretty_sorted_json(payload) + "\n", encoding="utf-8")
        return path

    def write_preflight_text(self, relative_path: str, content: str) -> Path:
        path = self._preflight_artifact_path(relative_path)
        path.write_text(content, encoding="utf-8")
        return path

    def _preflight_artifact_path(self, relative_path: str) -> Path:
        normalized_ref = Path(relative_path)
        if normalized_ref.is_absolute() or ".." in normalized_ref.parts:
            raise ValueError(f"Invalid preflight artifact path '{relative_path}'.")
        path = self.stages_dir / "preflight" / normalized_ref
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def workflow_signature_exists(
        self,
        workflow_name: str,
        workflow_signature: str,
    ) -> bool:
        return filesystem_manifest_exists(
            base_dir=self.base_dir,
            task_name=workflow_name,
            workflow_signature=workflow_signature,
        )

    def write_manifest(
        self,
        workflow_signature: str,
        manifest_data: JsonObject,
    ) -> Path:
        safe_workflow_signature = _validate_workflow_signature(workflow_signature)
        manifests_dir = self._directories.ensure_manifests_dir()
        manifest_file = manifests_dir / f"{safe_workflow_signature}.json"
        manifest_file.write_text(
            json.dumps(manifest_data, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        latest_file = manifests_dir / "latest.json"
        latest_file.write_text(
            json.dumps(
                {
                    "workflow_signature": safe_workflow_signature,
                    "manifest_file": manifest_file.name,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return manifest_file
