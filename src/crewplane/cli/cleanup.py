from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, cast

import typer
from rich.console import Console

from crewplane.architecture.contracts import NodeArtifactRequest
from crewplane.architecture.safe_files import (
    contained_directory,
    contained_regular_file,
)
from crewplane.artifacts.locks import run_lock_activity
from crewplane.artifacts.locks.manifest import LockManifestError, LockRunMetadata
from crewplane.artifacts.locks.process_identity import ProcessInspector
from crewplane.artifacts.locks.provider_processes import (
    ensure_no_live_provider_processes,
)
from crewplane.artifacts.workspace.node_state import refresh_node_workspace_descriptor
from crewplane.core.config import Settings, load_config
from crewplane.core.execution_state import RunManifest
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.core.state_paths import STATE_DIR_NAME, project_root_from_config_path
from crewplane.core.workspace.cache import workspace_cache_root
from crewplane.core.workspace.repository_identity import workspace_repository_id
from crewplane.runtime.workspace.cleanup import (
    WorkspaceCleanupEligibility,
    WorkspaceCleanupEligibilityLookup,
    WorkspaceCleanupFilter,
    WorkspaceCleanupResult,
    cleanup_workspace_cache,
    parse_duration_seconds,
    status_matches,
)
from crewplane.runtime.workspace.worktree.ref_cleanup import (
    WorkspaceRunRefCleanup,
    workspace_ref_cleanup_for_project,
)

from .paths import resolve_state_file
from .run.workspace.cache_policy import paths_overlap
from .run.workspace.git_source import (
    GitSourceContext,
    discover_git_context,
)
from .run.workspace.source_types import WorkspacePolicyBuilder
from .workspace_cleanup_evidence import WorkspaceCleanupEvidence

cleanup_app = typer.Typer(help="Remove generated crewplane runtime state.")


@dataclass(frozen=True)
class CleanupScope:
    repository_id: str | None
    git_context: GitSourceContext | None


@dataclass(frozen=True)
class _WorkspaceCleanupContext:
    project_root: Path
    scope: CleanupScope
    cache_root: Path
    statuses: frozenset[str]
    run_key_name: str | None
    older_than_seconds: int | None
    all_projects: bool
    orphans: bool


@dataclass(frozen=True)
class _CleanupNodeArtifactStateStore:
    stages_dir: Path

    def get_node_dir(self, request: NodeArtifactRequest) -> Path | None:
        stage_path = request.contract.stage_path
        if stage_path is None:
            return None
        return contained_directory(self.stages_dir, stage_path)


@cleanup_app.command("workspaces")
def cleanup_workspaces(
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            file_okay=True,
            dir_okay=False,
            help="Path to config file (default: .crewplane/config.yml)",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show workspaces that would be removed."),
    ] = False,
    run_key_name: Annotated[
        str | None,
        typer.Option("--run", help="Only clean workspaces for this run key."),
    ] = None,
    older_than: Annotated[
        str | None,
        typer.Option(
            "--older-than",
            help="Only clean entries older than a duration like 30m, 12h, or 7d.",
        ),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm destructive workspace cleanup."),
    ] = False,
    successful: Annotated[
        bool,
        typer.Option("--successful", help="Only clean succeeded workspace states."),
    ] = False,
    failed: Annotated[
        bool,
        typer.Option("--failed", help="Only clean failed workspace states."),
    ] = False,
    cancelled: Annotated[
        bool,
        typer.Option("--cancelled", help="Only clean cancelled workspace states."),
    ] = False,
    orphans: Annotated[
        bool,
        typer.Option(
            "--orphans", help="Only clean cache paths without workspace state."
        ),
    ] = False,
    all_projects: Annotated[
        bool,
        typer.Option(
            "--all-projects",
            help="Clean every repository bucket under the workspace cache.",
        ),
    ] = False,
) -> None:
    console = Console()
    try:
        context = resolve_cleanup_workspace_context(
            console=console,
            config_file=config_file,
            successful=successful,
            failed=failed,
            cancelled=cancelled,
            all_projects=all_projects,
            run_key_name=run_key_name,
            older_than=older_than,
            orphans=orphans,
        )
        destructive = yes and not dry_run
        warn_all_projects_cleanup(console, context.all_projects)
        result = execute_workspace_cleanup(context, destructive)
        write_cleanup_result(console, result, destructive)
    except Exception as exc:
        console.print(f"[red]Cleanup failed:[/] {exc}")
        raise typer.Exit(code=1) from exc


def resolve_cleanup_workspace_context(
    console: Console,
    config_file: Path | None,
    successful: bool,
    failed: bool,
    cancelled: bool,
    all_projects: bool,
    run_key_name: str | None,
    older_than: str | None,
    orphans: bool,
) -> _WorkspaceCleanupContext:
    resolved_config_file = resolve_state_file(
        config_file,
        "config.yml",
        "Run 'crewplane init' first.",
        console,
    )
    config = load_config(resolved_config_file)
    project_root = project_root_for_config(resolved_config_file)
    settings = config.settings
    statuses = cleanup_statuses(successful, failed, cancelled)
    validate_all_projects_filters(all_projects, statuses, orphans)
    scope = cleanup_scope(project_root, all_projects)
    cache_root = validate_cleanup_cache_root(
        settings,
        project_root,
        scope.git_context,
    )
    older_than_seconds = parse_duration_seconds(older_than)
    return _WorkspaceCleanupContext(
        project_root=project_root,
        scope=scope,
        cache_root=cache_root,
        statuses=statuses,
        run_key_name=run_key_name,
        older_than_seconds=older_than_seconds,
        all_projects=all_projects,
        orphans=orphans,
    )


def execute_workspace_cleanup(
    context: _WorkspaceCleanupContext,
    destructive: bool,
) -> WorkspaceCleanupResult:
    evidence = _workspace_cleanup_evidence(context)
    cleanup_filter = workspace_cleanup_filter(context)
    absent_state_projections = _eligible_absent_state_projections(context, evidence)
    inactive_ref_cleanup_run_keys = _inactive_ref_cleanup_runs(
        context.project_root,
        context.run_key_name,
        evidence,
    )
    ref_cleanup_run_keys = (
        ()
        if context.statuses or context.orphans or context.older_than_seconds is not None
        else inactive_ref_cleanup_run_keys
    )
    try:
        result = cleanup_workspace_cache(
            context.cache_root,
            cleanup_filter,
            dry_run=not destructive,
            status_lookup=(
                unknown_workspace_status_lookup
                if evidence is None
                else evidence.status_for_cache_key
            ),
            ref_cleanup=workspace_ref_cleanup(
                context.project_root,
                context.all_projects,
            ),
            eligibility_lookup=_workspace_cleanup_eligibility_lookup(
                context.project_root,
                context.all_projects,
                context.orphans,
                evidence,
            ),
            ref_cleanup_run_keys=ref_cleanup_run_keys,
            absent_state_projections=absent_state_projections,
        )
    except Exception as cleanup_error:
        if destructive:
            try:
                _refresh_cleanup_workspace_descriptors(
                    context.project_root,
                    inactive_ref_cleanup_run_keys,
                )
            except Exception as refresh_error:
                cleanup_error.add_note(
                    "Workspace descriptor refresh after partial cleanup failed: "
                    f"{refresh_error}"
                )
        raise
    if destructive:
        refresh_run_keys = set(ref_cleanup_run_keys)
        refresh_run_keys.update(
            entry.run_key_name for entry in result.entries if entry.removed
        )
        refresh_run_keys.update(
            run_key_name
            for run_key_name, _path, status, _state_paths in absent_state_projections
            if (
                cleanup_filter.run_key_name is None
                or cleanup_filter.run_key_name == run_key_name
            )
            and cleanup_filter.older_than_seconds is None
            and status_matches(status, cleanup_filter)
        )
        _refresh_cleanup_workspace_descriptors(
            context.project_root,
            tuple(sorted(refresh_run_keys)),
        )
    return result


def _refresh_cleanup_workspace_descriptors(
    project_root: Path,
    run_key_names: tuple[str, ...],
) -> None:
    state_dir = project_root / STATE_DIR_NAME
    for run_key_name in run_key_names:
        manifest_path = _run_manifest_path(state_dir, run_key_name)
        if manifest_path is None:
            continue
        run_dir = manifest_path.parent.parent
        node_manifest_dir = contained_directory(run_dir, "manifests/nodes")
        if node_manifest_dir is None or not any(node_manifest_dir.iterdir()):
            continue
        plan = _load_cleanup_preflight_plan(run_dir, manifest_path, run_key_name)
        store = _CleanupNodeArtifactStateStore(run_dir)
        for node in plan.nodes:
            policy = node.workspace_policy
            if policy is not None and policy.enabled:
                refresh_node_workspace_descriptor(node, plan, store)


def _load_cleanup_preflight_plan(
    run_dir: Path,
    manifest_path: Path,
    run_key_name: str,
) -> PreflightExecutionPlan:
    manifest = _read_workspace_manifest(manifest_path)
    if isinstance(manifest, str) or manifest.run_key_name != run_key_name:
        raise RuntimeError(
            f"Cannot refresh workspace descriptors for '{run_key_name}'."
        )
    plan_path = contained_regular_file(run_dir, manifest.preflight_plan_path)
    if plan_path is None:
        raise RuntimeError(f"Preflight plan is missing for run '{run_key_name}'.")
    try:
        plan = PreflightExecutionPlan.model_validate_json(
            plan_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"Preflight plan is invalid for run '{run_key_name}'."
        ) from exc
    if (
        plan.run_id != manifest.run_id
        or plan.run_key_name != manifest.run_key_name
        or plan.workflow_name != manifest.workflow_name
        or plan.workflow_signature != manifest.workflow_signature
    ):
        raise RuntimeError(
            f"Preflight plan identity is invalid for run '{run_key_name}'."
        )
    return plan


def _inactive_ref_cleanup_runs(
    project_root: Path,
    requested_run_key: str | None,
    evidence: WorkspaceCleanupEvidence | None,
) -> tuple[str, ...]:
    if evidence is None:
        return ()
    state_dir = project_root / STATE_DIR_NAME
    inspector = ProcessInspector()
    inactive: list[str] = []
    for run_key_name in evidence.ref_cleanup_run_keys():
        if requested_run_key is not None and run_key_name != requested_run_key:
            continue
        manifest_path = _run_manifest_path(state_dir, run_key_name)
        if manifest_path is None:
            continue
        manifest = _read_workspace_manifest(manifest_path)
        if (
            isinstance(manifest, str)
            or manifest.run_key_name != run_key_name
            or manifest.status == "running"
        ):
            continue
        if _run_lock_blocker(state_dir, run_key_name, inspector) is not None:
            continue
        if _provider_process_blocker(state_dir, manifest, inspector) is not None:
            continue
        inactive.append(run_key_name)
    return tuple(inactive)


def _eligible_absent_state_projections(
    context: _WorkspaceCleanupContext,
    evidence: WorkspaceCleanupEvidence | None,
) -> tuple[tuple[str, Path, str, tuple[Path, ...]], ...]:
    if evidence is None:
        return ()
    state_dir = context.project_root / STATE_DIR_NAME
    inspector = ProcessInspector()
    return tuple(
        projection
        for projection in evidence.absent_state_projections()
        if _first_cleanup_blocker_reason(
            projection[0],
            projection[2],
            state_dir,
            context.orphans,
            inspector,
        )
        is None
    )


def _workspace_cleanup_evidence(
    context: _WorkspaceCleanupContext,
) -> WorkspaceCleanupEvidence | None:
    if context.all_projects:
        return None
    git_context = cast(GitSourceContext, context.scope.git_context)
    repository_id_value = cast(str, context.scope.repository_id)
    return WorkspaceCleanupEvidence(
        context.project_root / STATE_DIR_NAME / "execution-stages",
        context.cache_root,
        repository_id_value,
        git_context.common_git_dir,
    )


def write_cleanup_result(
    console: Console,
    result: WorkspaceCleanupResult,
    destructive: bool,
) -> None:
    verb = "Removed" if destructive else "Would remove"
    console.print(
        f"{verb} {result.selected_count} workspace path(s) under "
        f"{result.cache_root.as_posix()}."
    )
    for entry in result.entries:
        console.print(
            f"  - {entry.path.as_posix()} "
            f"({entry.size_bytes} bytes, run={entry.run_key_name}, "
            f"status={entry.status or 'orphan'})"
        )
        if entry.retained_reason is not None:
            console.print(f"    retained: {entry.retained_reason}")
    if destructive and result.removed_ref_count:
        console.print(f"Removed {result.removed_ref_count} run-owned Git ref(s).")


def warn_all_projects_cleanup(console: Console, all_projects: bool) -> None:
    if not all_projects:
        return
    console.print(
        "[yellow]Warning:[/] --all-projects cannot verify cross-project run "
        "ownership or activity."
    )
    console.print(
        "Passing --yes without --dry-run explicitly authorizes deletion "
        "across every repository bucket."
    )


def workspace_cleanup_filter(
    context: _WorkspaceCleanupContext,
) -> WorkspaceCleanupFilter:
    return WorkspaceCleanupFilter(
        run_key_name=context.run_key_name,
        repository_id=context.scope.repository_id,
        expected_common_git_dir=(
            cast(GitSourceContext, context.scope.git_context).common_git_dir
            if not context.all_projects
            else None
        ),
        older_than_seconds=context.older_than_seconds,
        statuses=context.statuses,
        orphans=context.orphans,
    )


def workspace_ref_cleanup(
    project_root: Path,
    all_projects: bool,
) -> WorkspaceRunRefCleanup | None:
    if all_projects:
        return None
    return workspace_ref_cleanup_for_project(project_root)


def cleanup_statuses(
    successful: bool,
    failed: bool,
    cancelled: bool,
) -> frozenset[str]:
    statuses: set[str] = set()
    if successful:
        statuses.add("succeeded")
    if failed:
        statuses.add("failed")
    if cancelled:
        statuses.add("cancelled")
    return frozenset(statuses)


def _workspace_cleanup_eligibility_lookup(
    project_root: Path,
    all_projects: bool,
    orphan_cleanup_requested: bool,
    evidence: WorkspaceCleanupEvidence | None = None,
) -> WorkspaceCleanupEligibilityLookup | None:
    """Return project-scoped cleanup checks or no cross-project override."""
    if all_projects:
        return None

    state_dir = project_root / STATE_DIR_NAME
    inspector = ProcessInspector()

    def current_project_lookup(
        run_key_name: str,
        workspace_path: Path,
        status: str | None,
    ) -> WorkspaceCleanupEligibility:
        evidence_decision = (
            evidence.decision(run_key_name, workspace_path)
            if evidence is not None
            else None
        )
        if evidence_decision is not None and not evidence_decision.deletable:
            return WorkspaceCleanupEligibility(
                deletable=False,
                reason=evidence_decision.reason,
                state_paths=evidence_decision.state_paths,
                expected_worktree_git_dir=(evidence_decision.expected_worktree_git_dir),
            )
        blocker_reason = _first_cleanup_blocker_reason(
            run_key_name,
            status,
            state_dir,
            orphan_cleanup_requested,
            inspector,
        )
        return WorkspaceCleanupEligibility(
            deletable=blocker_reason is None,
            reason=blocker_reason,
            state_paths=(
                evidence_decision.state_paths if evidence_decision is not None else ()
            ),
            expected_worktree_git_dir=(
                evidence_decision.expected_worktree_git_dir
                if evidence_decision is not None
                else None
            ),
        )

    return current_project_lookup


def _first_cleanup_blocker_reason(
    run_key_name: str,
    status: str | None,
    state_dir: Path,
    orphan_cleanup_requested: bool,
    inspector: ProcessInspector,
) -> str | None:
    workspace_state_reason = _workspace_state_blocker_reason(
        status,
        orphan_cleanup_requested,
    )
    if workspace_state_reason is not None:
        return workspace_state_reason

    manifest, manifest_reason = _load_workspace_manifest(state_dir, run_key_name)
    if manifest is None:
        return manifest_reason

    run_lock_reason = _run_lock_blocker(state_dir, run_key_name, inspector)
    if run_lock_reason is not None:
        return run_lock_reason

    provider_process_reason = _provider_process_blocker(
        state_dir,
        manifest,
        inspector,
    )
    if provider_process_reason is not None:
        return provider_process_reason

    return None


def _workspace_state_blocker_reason(
    workspace_state: str | None,
    orphan_cleanup_requested: bool,
) -> str | None:
    if workspace_state is None:
        if orphan_cleanup_requested:
            return None
        return "workspace state is unverifiable"
    if workspace_state not in {"succeeded", "failed", "cancelled"}:
        return f"workspace state is {workspace_state}"
    return None


def _load_workspace_manifest(
    state_dir: Path,
    run_key_name: str,
) -> tuple[RunManifest | None, str | None]:
    manifest_path = _run_manifest_path(state_dir, run_key_name)
    if manifest_path is None:
        return None, "run manifest is missing or unsafe"

    manifest = _read_workspace_manifest(manifest_path)
    if isinstance(manifest, str):
        return None, manifest

    return _validate_workspace_manifest(manifest, run_key_name)


def _run_manifest_path(state_dir: Path, run_key_name: str) -> Path | None:
    try:
        return contained_regular_file(
            state_dir / "execution-stages",
            f"{run_key_name}/manifests/run.json",
        )
    except OSError:
        return None


def _read_workspace_manifest(
    manifest_path: Path,
) -> RunManifest | str:
    try:
        manifest = RunManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return "run manifest is invalid"

    return manifest


def _validate_workspace_manifest(
    manifest: RunManifest,
    run_key_name: str,
) -> tuple[RunManifest | None, str | None]:
    if manifest.run_key_name != run_key_name or manifest.status == "running":
        return None, "run manifest is active or mismatched"

    return manifest, None


def _run_lock_blocker(
    state_dir: Path,
    run_key_name: str,
    inspector: ProcessInspector,
) -> str | None:
    try:
        lock_activity = run_lock_activity(state_dir, run_key_name, inspector)
    except (OSError, RuntimeError):
        lock_activity = "unverifiable"

    if lock_activity in {"live", "unverifiable"}:
        return f"run lock is {lock_activity}"

    return None


def _provider_process_blocker(
    state_dir: Path,
    manifest: RunManifest,
    inspector: ProcessInspector,
) -> str | None:
    metadata = LockRunMetadata(
        run_id=manifest.run_id,
        run_key_name=manifest.run_key_name,
        workflow_identity=manifest.workflow_identity,
        workflow_signature=manifest.workflow_signature,
    )
    try:
        ensure_no_live_provider_processes(state_dir, metadata, inspector)
    except (LockManifestError, OSError, RuntimeError) as exc:
        return str(exc) or "provider process state is unverifiable"

    return None


def project_root_for_config(config_file: Path) -> Path:
    return project_root_from_config_path(config_file)


def cleanup_repository_id(project_root: Path, all_projects: bool) -> str | None:
    return cleanup_scope(project_root, all_projects).repository_id


def cleanup_scope(project_root: Path, all_projects: bool) -> CleanupScope:
    builder = WorkspacePolicyBuilder()
    git_context = discover_git_context(project_root, builder)
    if all_projects:
        return CleanupScope(repository_id=None, git_context=git_context)
    if git_context is None:
        diagnostics = "; ".join(builder.errors)
        suffix = f" {diagnostics}" if diagnostics else ""
        raise RuntimeError(
            "Workspace cleanup is scoped to the current Git repository by default."
            f"{suffix} Use --all-projects to clean every repository bucket."
        )
    return CleanupScope(
        repository_id=workspace_repository_id(
            git_context.common_git_dir,
            project_root,
            git_context.object_format,
        ),
        git_context=git_context,
    )


def validate_all_projects_filters(
    all_projects: bool,
    statuses: frozenset[str],
    orphans: bool,
) -> None:
    if not all_projects or (not statuses and not orphans):
        return
    raise RuntimeError(
        "--all-projects cannot be combined with --orphans or status filters "
        "(--successful, --failed, --cancelled) because those filters require "
        "current-project workspace-state artifacts."
    )


def validate_cleanup_cache_root(
    settings: Settings,
    project_root: Path,
    git_context: GitSourceContext | None,
) -> Path:
    cache_root = workspace_cache_root(settings.workspace.cache_root)
    if not cache_root.is_absolute():
        raise RuntimeError(
            "settings.workspace.cache_root must be absolute for workspace cleanup."
        )
    if cache_root.is_symlink():
        raise RuntimeError(
            f"Workspace cache root must not be a symlink: {cache_root.as_posix()}"
        )
    state_dir = project_root / STATE_DIR_NAME
    blocked_roots = [
        project_root,
        state_dir,
        state_dir / "execution-stages",
        state_dir / "execution-results",
        state_dir / "locks",
    ]
    if git_context is not None:
        blocked_roots.extend([git_context.active_git_dir, git_context.common_git_dir])
    for blocked in blocked_roots:
        if paths_overlap(cache_root, blocked):
            raise RuntimeError(
                "Workspace cache root must not overlap the project, .crewplane, "
                f"or Git metadata paths: {cache_root.as_posix()}"
            )
    return cache_root


def unknown_workspace_status_lookup(
    run_key_name: str,  # noqa: ARG001 - Required by the cleanup callback.
    cache_key: str,  # noqa: ARG001 - Required by the cleanup callback.
) -> str:
    return "unknown"
