from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from orchestrator_cli.architecture.contracts import (
    CanonicalIntegrationConfig,
    JsonObject,
)
from orchestrator_cli.core.preflight.models import PreflightExecutionPlan


@dataclass(frozen=True)
class StageFinalizeResult:
    """Summary of the consolidated artifacts produced for a workflow stage."""

    stage_name: str
    result_file: Path
    findings_file: Path | None
    included_outputs: tuple[Path, ...]
    skipped_empty_outputs: tuple[Path, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class StageTaskSpec:
    """Ordered task metadata used to finalize stage artifacts deterministically."""

    task_id: str
    role: str


class ArtifactStorePort(Protocol):
    """Runtime-facing artifact store used during a single workflow run."""

    run_id: str
    task_name: str
    stages_dir: Path
    results_dir: Path
    logs_dir: Path
    log_cli_output: bool

    def create_stage_dir(self, stage_name: str) -> Path:
        """Create and return the writable directory for a node stage."""

    def get_stage_dir(self, stage_name: str) -> Path | None:
        """Return the current run's stage directory if it has been created."""

    def finalize_stage(
        self,
        stage_name: str,
        findings_enabled: bool = False,
        task_specs: tuple[StageTaskSpec, ...] = (),
    ) -> StageFinalizeResult:
        """Consolidate a stage's run artifacts into result artifacts."""

    def get_run_log_dir(self) -> Path:
        """Return the run-level log directory, creating it when needed."""

    def get_orchestrator_event_log_path(self) -> Path:
        """Return the run-level structured event log path."""

    def get_orchestrator_summary_path(self) -> Path:
        """Return the run-level human-readable summary path."""

    def get_log_file(
        self,
        stage_name: str,
        provider: str,
        task_id: str,
        audit_round_num: int | None = None,
        round_num: int | None = None,
    ) -> Path | None:
        """Return the per-invocation log path, or None when capture is disabled."""

    def get_stage_output_path(self, stage_name: str) -> Path:
        """Return the consolidated output artifact path for a stage."""

    def get_stage_findings_path(self, stage_name: str) -> Path:
        """Return the consolidated findings artifact path for a stage."""

    def write_preflight_plan(self, plan: PreflightExecutionPlan) -> Path:
        """Persist the successful preflight execution plan."""

    def write_preflight_static_file(self, content_ref: str, payload: bytes) -> Path:
        """Persist bundled static content read by preflight."""

    def write_preflight_manifest(self, payload: object) -> Path:
        """Persist the preflight status manifest."""

    def write_preflight_diagnostics(self, payload: object) -> Path:
        """Persist preflight diagnostics."""

    def write_preflight_metadata(self, payload: object) -> Path:
        """Persist preflight run metadata."""

    def write_preflight_summary(self, content: str) -> Path:
        """Persist the human-readable preflight summary."""

    def write_preflight_render_plan(self, payload: object) -> Path:
        """Persist successful render-plan metadata."""

    def write_preflight_execution_bundle(self, payload: object) -> Path:
        """Persist successful compiled execution-bundle metadata."""

    def write_preflight_json(self, relative_path: str, payload: object) -> Path:
        """Persist a JSON preflight artifact under the run preflight directory."""

    def write_preflight_text(self, relative_path: str, content: str) -> Path:
        """Persist a text preflight artifact under the run preflight directory."""

    def workflow_signature_exists(
        self, workflow_name: str, workflow_signature: str
    ) -> bool:
        """Return whether a successful manifest exists for this workflow signature."""

    def write_manifest(
        self, workflow_signature: str, manifest_data: JsonObject
    ) -> Path:
        """Persist a run manifest and return its path."""


class ArtifactAdapterPort(Protocol):
    """Factory contract for artifact storage integrations."""

    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: JsonObject | None = None,
    ) -> CanonicalIntegrationConfig:
        """Validate and canonicalize artifact options without side effects."""

    def workflow_signature_exists(
        self,
        workflow_name: str,
        orchestrator_dir: Path,
        options: JsonObject | None,
        workflow_signature: str,
    ) -> bool:
        """Return whether a successful manifest exists without allocating a run."""

    def create_store(
        self,
        workflow_name: str,
        orchestrator_dir: Path,
        project_root: Path,
        options: JsonObject | None = None,
    ) -> ArtifactStorePort:
        """Build the artifact store for a concrete workflow run."""
