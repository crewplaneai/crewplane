from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rich.console import Console

from orchestrator_cli.architecture.errors import IntegrationResolutionError
from orchestrator_cli.architecture.loader import resolve_implementation_path
from orchestrator_cli.artifacts.resume_decision import (
    ResumeDecision,
    decide_same_context_action,
)
from orchestrator_cli.artifacts.resume_validation import (
    ValidatedResumeFrontier,
    validate_resume_frontier,
)
from orchestrator_cli.artifacts.run_history import find_same_context_runs
from orchestrator_cli.core.config import Config, Settings
from orchestrator_cli.core.preflight import (
    PreflightCompilationPreview,
    PreflightExecutionPlan,
)
from orchestrator_cli.core.preflight.source import PreflightWorkflowSource

FILESYSTEM_ARTIFACT_ADAPTER = (
    "orchestrator_cli.adapters.artifacts.filesystem:FilesystemArtifactsAdapter"
)


@dataclass(frozen=True)
class ResumePlan:
    workflow_identity: str
    decision: ResumeDecision
    frontier: ValidatedResumeFrontier | None = None

    @property
    def resumed_node_ids(self) -> tuple[str, ...]:
        if self.frontier is None:
            return ()
        return self.frontier.resumed_node_ids


def require_filesystem_artifacts_backend(config: Config) -> None:
    if filesystem_artifacts_backend_enabled(config):
        return
    raise RuntimeError(
        "Artifact-backed skip/resume requires the built-in filesystem artifacts "
        "backend in this release."
    )


def filesystem_artifacts_backend_enabled(config: Config) -> bool:
    settings = config.settings if config.settings is not None else Settings()
    implementation = settings.integrations.artifacts.implementation
    try:
        resolved = resolve_implementation_path("artifacts", implementation)
    except IntegrationResolutionError:
        return False
    return _normalize_object_path(resolved) == _normalize_object_path(
        FILESYSTEM_ARTIFACT_ADAPTER
    )


def workflow_identity_for_source(
    source: PreflightWorkflowSource,
    project_root: Path,
) -> str:
    if source.root_workflow_path is None:
        raise ValueError("Workflow source is missing root_workflow_path for resume.")
    resolved_path = source.root_workflow_path.resolve(strict=False)
    resolved_root = project_root.resolve(strict=False)
    try:
        return resolved_path.relative_to(resolved_root).as_posix()
    except ValueError:
        return resolved_path.as_posix()


def build_resume_plan(
    config: Config,
    source: PreflightWorkflowSource,
    preview: PreflightCompilationPreview,
    project_root: Path,
    orchestrator_dir: Path,
    force: bool,
) -> ResumePlan:
    require_filesystem_artifacts_backend(config)
    workflow_identity = workflow_identity_for_source(source, project_root)
    if preview.workflow_signature is None or preview.workflow_name is None:
        raise ValueError("Successful preflight preview is required for resume.")
    if force:
        return ResumePlan(
            workflow_identity=workflow_identity,
            decision=ResumeDecision(kind="execute_full"),
        )
    records = find_same_context_runs(
        orchestrator_dir=orchestrator_dir,
        workflow_identity=workflow_identity,
        workflow_name=preview.workflow_name,
        workflow_signature=preview.workflow_signature,
    )
    decision = decide_same_context_action(records, force)
    if decision.kind != "resume" or decision.resume_source is None:
        return ResumePlan(workflow_identity=workflow_identity, decision=decision)
    validation_plan = _preview_plan_for_validation(preview)
    frontier = validate_resume_frontier(decision.resume_source, validation_plan)
    if not frontier.resumed_node_ids:
        return ResumePlan(
            workflow_identity=workflow_identity,
            decision=ResumeDecision(kind="execute_full"),
        )
    return ResumePlan(
        workflow_identity=workflow_identity,
        decision=decision,
        frontier=frontier,
    )


def print_dry_run_resume_advisory(
    config: Config,
    source: PreflightWorkflowSource,
    preview: PreflightCompilationPreview,
    project_root: Path,
    orchestrator_dir: Path,
    force: bool,
    console: Console,
) -> None:
    if not filesystem_artifacts_backend_enabled(config):
        console.print(
            "Resume advisory: unavailable for non-filesystem artifact backends."
        )
        return
    try:
        resume_plan = build_resume_plan(
            config,
            source,
            preview,
            project_root,
            orchestrator_dir,
            force,
        )
    except ValueError as exc:
        console.print(f"Resume advisory: unavailable ({exc})")
        return
    match resume_plan.decision.kind:
        case "skip":
            console.print("Resume advisory: would_skip")
        case "resume":
            source_run = resume_plan.decision.resume_source
            source_run_id = (
                source_run.manifest.run_id if source_run is not None else "unknown"
            )
            console.print(
                "Resume advisory: would_resume "
                f"{len(resume_plan.resumed_node_ids)} node(s) from {source_run_id}"
            )
        case "execute_full":
            console.print("Resume advisory: would_execute_full_run")
    if _uses_ephemeral_sensitive_fingerprints(preview):
        console.print(
            "Resume advisory: non-binding because sensitive fingerprints are ephemeral."
        )


def _preview_plan_for_validation(
    preview: PreflightCompilationPreview,
) -> PreflightExecutionPlan:
    return PreflightExecutionPlan.from_preview(
        preview=preview,
        run_id="dry-run",
        run_key_name="dry-run",
        context_root=".",
        manifest_root="./manifests",
        created_at=datetime.now(),
    )


def _uses_ephemeral_sensitive_fingerprints(
    preview: PreflightCompilationPreview,
) -> bool:
    metadata = preview.fingerprint_metadata
    return bool(metadata.get("sensitive_values_required")) and not bool(
        metadata.get("fingerprint_key_persisted")
    )


def _normalize_object_path(implementation: str) -> str:
    if ":" in implementation:
        module_name, object_name = implementation.split(":", 1)
        if module_name and object_name:
            return f"{module_name}:{object_name}"
    if "." in implementation:
        module_name, object_name = implementation.rsplit(".", 1)
        if module_name and object_name:
            return f"{module_name}:{object_name}"
    return implementation
