from __future__ import annotations

from pathlib import Path

from crewplane.architecture.contracts import (
    InvocationProcessEvent,
    JsonObject,
    NodeArtifactRequest,
    VerifiedNodeArtifact,
)
from crewplane.architecture.ports.artifacts import (
    ProviderProcessInvocation,
    ProviderProcessPublication,
    StageFinalizeResult,
    StageTaskSpec,
)
from crewplane.architecture.safe_files import (
    contained_directory,
    contained_regular_file,
    ensure_contained_directory,
)
from crewplane.core.execution_state import NodeState, RunManifest, RunStatus
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.core.preflight.serialization import pretty_sorted_json

from .atomic import (
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
)
from .directory_manager import DirectoryManager
from .naming import (
    build_log_filename,
    build_workspace_export_filename,
    node_state_relative_path,
    run_manifest_relative_path,
    safe_artifact_name,
)
from .provider_process_publisher import ProviderProcessPublisher
from .results.writer import ResultWriter
from .verification import read_verified_node_artifact


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
        self._provider_process_publisher = ProviderProcessPublisher(self._directories)
        self._node_artifact_requests: dict[str, NodeArtifactRequest] = {}

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

    def create_node_dir(self, request: NodeArtifactRequest) -> Path:
        stage_path = request.contract.stage_path
        if stage_path is None:
            raise ValueError("Compiled stage locator is missing.")
        stage_dir = ensure_contained_directory(
            self.stages_dir,
            stage_path,
        )
        self._node_artifact_requests[request.node_id] = request
        return stage_dir

    def get_node_dir(self, request: NodeArtifactRequest) -> Path | None:
        stage_path = request.contract.stage_path
        if stage_path is None:
            raise ValueError("Compiled stage locator is missing.")
        return contained_directory(
            self.stages_dir,
            stage_path,
        )

    def get_node_artifact_request(
        self,
        node_id: str,
    ) -> NodeArtifactRequest | None:
        return self._node_artifact_requests.get(node_id)

    def finalize_node(
        self,
        request: NodeArtifactRequest,
        findings_enabled: bool = False,
        task_specs: tuple[StageTaskSpec, ...] = (),
        generated_file_detection_enabled: bool = True,
        generated_file_workspace_roots: dict[Path, Path | None] | None = None,
    ) -> StageFinalizeResult:
        return self._result_writer.finalize_at(
            request.node_id,
            self.get_node_dir(request),
            self.get_node_output_path(request),
            self.get_node_findings_path(request),
            findings_enabled=findings_enabled,
            task_specs=task_specs,
            generated_file_detection_enabled=generated_file_detection_enabled,
            generated_file_workspace_roots=generated_file_workspace_roots,
        )

    def get_node_output_path(self, request: NodeArtifactRequest) -> Path:
        return self._compiled_path(
            self.results_dir,
            request.contract.output_path,
            "output",
        )

    def get_node_findings_path(self, request: NodeArtifactRequest) -> Path | None:
        if request.contract.findings_path is None:
            return None
        return self._compiled_path(
            self.results_dir,
            request.contract.findings_path,
            "findings",
        )

    def get_node_log_file(
        self,
        request: NodeArtifactRequest,
        provider: str,
        task_id: str,
        audit_round_num: int | None = None,
        round_num: int | None = None,
    ) -> Path | None:
        if not self.log_cli_output:
            return None
        log_path = request.contract.log_path
        if log_path is None:
            raise ValueError("Log capture requires a compiled log locator.")
        ensure_contained_directory(self.stages_dir, log_path)
        provider_dir = ensure_contained_directory(
            self.stages_dir,
            f"{log_path}/{safe_artifact_name(provider)}",
        )
        return provider_dir / build_log_filename(
            task_id,
            audit_round_num,
            round_num,
        )

    def read_verified_node_artifact(
        self,
        request: NodeArtifactRequest,
        kind: str,
    ) -> VerifiedNodeArtifact:
        if kind == "output":
            self.get_node_output_path(request)
        elif kind == "findings":
            self.get_node_findings_path(request)
        return read_verified_node_artifact(
            self.stages_dir,
            self.results_dir,
            request,
            kind,
        )

    def get_run_log_dir(self) -> Path:
        return self._directories.ensure_run_logs_dir()

    def get_run_event_log_path(self) -> Path:
        return self._directories.get_run_event_log_path()

    def get_run_summary_path(self) -> Path:
        return self._directories.get_run_summary_path()

    def write_preflight_plan(self, plan: PreflightExecutionPlan) -> Path:
        plan_path = self._preflight_artifact_path("execution-plan.json")
        return atomic_write_text(plan_path, pretty_sorted_json(plan) + "\n")

    def write_preflight_static_file(self, content_ref: str, payload: bytes) -> Path:
        path = self._preflight_artifact_path(content_ref)
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
        if (
            not relative_path
            or normalized_ref.is_absolute()
            or any(part in {"", ".", ".."} for part in relative_path.split("/"))
        ):
            raise ValueError(f"Invalid preflight artifact path '{relative_path}'.")
        preflight_dir = ensure_contained_directory(self.stages_dir, "preflight")
        parent = (
            preflight_dir
            if normalized_ref.parent == Path(".")
            else ensure_contained_directory(
                preflight_dir,
                normalized_ref.parent.as_posix(),
            )
        )
        path = parent / normalized_ref.name
        if path.is_symlink():
            raise ValueError(f"Preflight artifact path must not be a symlink: {path}")
        return path

    def write_run_manifest(self, manifest: RunManifest) -> Path:
        manifests_dir = self._directories.ensure_manifests_dir()
        return atomic_write_json(
            manifests_dir / run_manifest_relative_path().name,
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
        relative_path = node_state_relative_path(node_state.node_id)
        node_state_dir = ensure_contained_directory(
            self.stages_dir, relative_path.parent.as_posix()
        )
        node_state_path = node_state_dir / relative_path.name
        return atomic_write_json(
            node_state_path,
            node_state.model_dump(mode="json", exclude_none=True),
        )

    def write_provider_process_event(
        self,
        invocation: ProviderProcessInvocation,
        event: InvocationProcessEvent,
    ) -> ProviderProcessPublication:
        return self._provider_process_publisher.publish(invocation, event)

    def write_node_resume_source(
        self,
        request: NodeArtifactRequest,
        payload: JsonObject,
    ) -> Path:
        stage_dir = self.create_node_dir(request)
        return atomic_write_json(stage_dir / "resume-source.json", payload)

    def record_hydrated_resume_node(
        self,
        node_id: str,
        source_run_id: str,
        source_run_key_name: str,
    ) -> Path:
        manifest_path = self._run_manifest_path()
        current = RunManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        resumed_nodes = [*current.resumed_nodes, node_id]
        updated = current.model_copy(
            update={
                "resumed_nodes": resumed_nodes,
                "resume_source_run_id": source_run_id,
                "resume_source_run_key_name": source_run_key_name,
            }
        )
        validated = RunManifest.model_validate(updated.model_dump(mode="json"))
        return self.write_run_manifest(validated)

    def write_workspace_export(
        self, logical_worktree_name: str, payload: object
    ) -> Path:
        export_dir = ensure_contained_directory(self.stages_dir, "workspace-exports")
        export_name = build_workspace_export_filename(logical_worktree_name)
        return atomic_write_json(export_dir / export_name, payload)

    def _run_manifest_path(self) -> Path:
        manifest_path = contained_regular_file(
            self.stages_dir, run_manifest_relative_path().as_posix()
        )
        if manifest_path is None:
            raise ValueError("Run manifest is missing or is not a safe regular file.")
        return manifest_path

    def _compiled_path(
        self,
        root: Path,
        locator: str | None,
        label: str,
    ) -> Path:
        if locator is None:
            raise ValueError(f"Compiled {label} locator is missing.")
        path = Path(locator)
        if (
            not locator
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in locator.split("/"))
        ):
            raise ValueError(f"Invalid compiled {label} locator '{locator}'.")
        relative_root = root.relative_to(self.base_dir)
        relative_parent = relative_root / path.parent
        ensure_contained_directory(
            self.base_dir,
            relative_root.as_posix()
            if path.parent == Path(".")
            else relative_parent.as_posix(),
        )
        resolved = root / path
        if resolved.is_symlink():
            raise ValueError(f"Compiled {label} locator must not be a symlink.")
        return resolved
