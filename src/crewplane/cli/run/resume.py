from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rich.console import Console

from crewplane.architecture.errors import IntegrationResolutionError
from crewplane.architecture.loader import resolve_implementation_path
from crewplane.artifacts.resume.decision import (
    ResumeDecision,
    decide_same_context_action,
)
from crewplane.artifacts.resume.validation import (
    ValidatedResumeFrontier,
    validate_resume_frontier,
)
from crewplane.artifacts.run_history import (
    RunHistoryError,
    RunHistoryRecord,
    find_same_context_runs,
)
from crewplane.core.config import Config, Settings
from crewplane.core.preflight import (
    PreflightCompilationPreview,
    PreflightExecutionPlan,
)
from crewplane.core.preflight.source import PreflightWorkflowSource
from crewplane.core.preflight.workspace.observability import workspace_enabled
from crewplane.runtime.workspace.branch_export import (
    preview_branch_exports_from_history,
)

from .branch_export_output import print_branch_export_verifications

FILESYSTEM_ARTIFACT_ADAPTER = (
    "crewplane.adapters.artifacts.filesystem:FilesystemArtifactsAdapter"
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
        "Real execution requires the built-in filesystem artifacts backend in "
        "this release; non-filesystem artifact backends are limited to validate "
        "and dry-run."
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
    state_dir: Path,
    force: bool,
) -> ResumePlan:
    workflow_identity = workflow_identity_for_source(source, project_root)
    if preview.workflow_signature is None or preview.workflow_name is None:
        raise ValueError("Successful preflight preview is required for resume.")
    require_filesystem_artifacts_backend(config)
    if force:
        return ResumePlan(
            workflow_identity=workflow_identity,
            decision=ResumeDecision(kind="execute_full"),
        )
    if workspace_enabled(preview):
        records = find_same_context_runs(
            state_dir=state_dir,
            workflow_identity=workflow_identity,
            workflow_name=preview.workflow_name,
            workflow_signature=preview.workflow_signature,
        )
        return _workspace_resume_plan(
            workflow_identity,
            records,
            _preview_plan_for_validation(preview, project_root),
        )
    records = find_same_context_runs(
        state_dir=state_dir,
        workflow_identity=workflow_identity,
        workflow_name=preview.workflow_name,
        workflow_signature=preview.workflow_signature,
    )
    decision = decide_same_context_action(records, force)
    if decision.kind != "resume" or decision.resume_source is None:
        return ResumePlan(workflow_identity=workflow_identity, decision=decision)
    validation_plan = _preview_plan_for_validation(preview, project_root)
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
    state_dir: Path,
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
            state_dir,
            force,
        )
    except (ValueError, PermissionError, RunHistoryError) as exc:
        console.print(f"Resume advisory: unavailable ({exc})")
        return
    match resume_plan.decision.kind:
        case "skip":
            successful_run = resume_plan.decision.successful_run
            branch_export_records = ()
            if successful_run is not None:
                branch_plan = _preview_plan_for_run(
                    preview,
                    successful_run.manifest.run_id,
                    successful_run.manifest.run_key_name,
                    project_root,
                    successful_run.run_dir / "manifests",
                )
                branch_export_records = preview_branch_exports_from_history(
                    branch_plan,
                    successful_run,
                )
                if _branch_export_verification_failed(branch_export_records):
                    print_branch_export_verifications(branch_export_records, console)
                    console.print(
                        "Resume advisory: unavailable "
                        "(branch export verification failed)"
                    )
                    return
            console.print("Resume advisory: would_skip")
            if branch_export_records:
                print_branch_export_verifications(
                    branch_export_records,
                    console,
                )
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
    project_root: Path,
) -> PreflightExecutionPlan:
    return PreflightExecutionPlan.from_preview(
        preview=preview,
        run_id="dry-run",
        run_key_name="dry-run",
        project_root=project_root.as_posix(),
        context_root=".",
        manifest_root="./manifests",
        created_at=datetime.now(),
    )


def _branch_export_verification_failed(records: tuple[dict[str, object], ...]) -> bool:
    return any(record.get("status") == "failed_verification" for record in records)


def _preview_plan_for_run(
    preview: PreflightCompilationPreview,
    run_id: str,
    run_key_name: str,
    context_root: Path,
    manifest_root: Path,
) -> PreflightExecutionPlan:
    return PreflightExecutionPlan.from_preview(
        preview=preview,
        run_id=run_id,
        run_key_name=run_key_name,
        project_root=context_root.as_posix(),
        context_root=context_root.as_posix(),
        manifest_root=manifest_root.as_posix(),
        created_at=datetime.now(),
    )


def _workspace_resume_plan(
    workflow_identity: str,
    records: tuple[RunHistoryRecord, ...],
    validation_plan: PreflightExecutionPlan,
) -> ResumePlan:
    for record in records:
        if record.manifest.status != "succeeded":
            continue
        frontier = validate_resume_frontier(record, validation_plan)
        if len(frontier.resumed_node_ids) == len(validation_plan.nodes):
            return ResumePlan(
                workflow_identity=workflow_identity,
                decision=ResumeDecision(kind="skip", successful_run=record),
            )
    for record in records:
        if record.manifest.status not in {"failed", "cancelled"}:
            continue
        frontier = validate_resume_frontier(record, validation_plan)
        if frontier.resumed_node_ids:
            return ResumePlan(
                workflow_identity=workflow_identity,
                decision=ResumeDecision(kind="resume", resume_source=record),
                frontier=frontier,
            )
    return ResumePlan(
        workflow_identity=workflow_identity,
        decision=ResumeDecision(kind="execute_full"),
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
