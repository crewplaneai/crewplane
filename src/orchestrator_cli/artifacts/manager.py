from __future__ import annotations

from pathlib import Path

from orchestrator_cli.architecture.contracts import JsonObject
from orchestrator_cli.architecture.ports.artifacts import (
    StageFinalizeResult,
    StageTaskSpec,
)
from orchestrator_cli.core.execution_state import NodeState, RunManifest, RunStatus
from orchestrator_cli.core.preflight.models import PreflightExecutionPlan
from orchestrator_cli.core.preflight.serialization import pretty_sorted_json

from .atomic import atomic_write_bytes, atomic_write_json, atomic_write_text
from .directory_manager import DirectoryManager, safe_artifact_name
from .naming import build_node_state_filename, build_workspace_export_filename
from .result_writer import ResultWriter


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
    def run_key_name(self) -> str:
        return self._directories.run_key_name

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
        generated_file_detection_enabled: bool = True,
        generated_file_workspace_roots: dict[Path, Path] | None = None,
    ) -> StageFinalizeResult:
        return self._result_writer.finalize_stage(
            stage_name,
            self.get_stage_dir(stage_name),
            findings_enabled=findings_enabled,
            task_specs=task_specs,
            generated_file_detection_enabled=generated_file_detection_enabled,
            generated_file_workspace_roots=generated_file_workspace_roots,
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
        return atomic_write_text(plan_path, pretty_sorted_json(plan) + "\n")

    def write_preflight_static_file(self, content_ref: str, payload: bytes) -> Path:
        normalized_ref = Path(content_ref)
        if normalized_ref.is_absolute() or ".." in normalized_ref.parts:
            raise ValueError(f"Invalid preflight content reference '{content_ref}'.")
        path = self.stages_dir / "preflight" / normalized_ref
        return atomic_write_bytes(path, payload)

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
        return atomic_write_text(path, pretty_sorted_json(payload) + "\n")

    def write_preflight_text(self, relative_path: str, content: str) -> Path:
        path = self._preflight_artifact_path(relative_path)
        return atomic_write_text(path, content)

    def _preflight_artifact_path(self, relative_path: str) -> Path:
        normalized_ref = Path(relative_path)
        if normalized_ref.is_absolute() or ".." in normalized_ref.parts:
            raise ValueError(f"Invalid preflight artifact path '{relative_path}'.")
        path = self.stages_dir / "preflight" / normalized_ref
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def write_run_manifest(self, manifest: RunManifest) -> Path:
        manifests_dir = self._directories.ensure_manifests_dir()
        return atomic_write_json(
            manifests_dir / "run.json",
            manifest.model_dump(mode="json", exclude_none=True),
        )

    def update_run_manifest_status(
        self,
        status: RunStatus,
        completed_at: str,
        failure_message: str | None = None,
        cancel_reason: str | None = None,
    ) -> Path:
        manifest_path = self._run_manifest_path()
        current = RunManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        updated = current.model_copy(
            update={
                "status": status,
                "completed_at": completed_at,
                "failure_message": failure_message,
                "cancel_reason": cancel_reason,
            }
        )
        validated = RunManifest.model_validate(updated.model_dump(mode="json"))
        return self.write_run_manifest(validated)

    def write_node_success_state(self, node_state: NodeState) -> Path:
        node_state_dir = self._directories.ensure_manifests_dir() / "nodes"
        node_state_path = node_state_dir / build_node_state_filename(node_state.node_id)
        return atomic_write_json(
            node_state_path,
            node_state.model_dump(mode="json", exclude_none=True),
        )

    def write_resume_source(self, node_id: str, payload: JsonObject) -> Path:
        stage_dir = self.create_stage_dir(node_id)
        return atomic_write_json(stage_dir / "resume-source.json", payload)

    def write_workspace_export(
        self, logical_worktree_name: str, payload: object
    ) -> Path:
        export_dir = self.stages_dir / "workspace-exports"
        export_name = build_workspace_export_filename(logical_worktree_name)
        return atomic_write_json(export_dir / export_name, payload)

    def _run_manifest_path(self) -> Path:
        return self._directories.ensure_manifests_dir() / "run.json"
