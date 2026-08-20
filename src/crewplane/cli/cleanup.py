from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, cast

import typer
from rich.console import Console

from crewplane.architecture.safe_files import contained_regular_file
from crewplane.artifacts.locks import run_lock_activity
from crewplane.artifacts.locks.manifest import LockManifestError, LockRunMetadata
from crewplane.artifacts.locks.process_identity import ProcessInspector
from crewplane.artifacts.locks.provider_processes import (
    ensure_no_live_provider_processes,
)
from crewplane.core.config import Settings, load_config
from crewplane.core.execution_state import RunManifest
from crewplane.core.state_paths import STATE_DIR_NAME, project_root_from_config_path
from crewplane.core.workspace.cache import workspace_cache_root
from crewplane.runtime.workspace.cleanup import (
    WorkspaceCleanupEligibility,
    WorkspaceCleanupEligibilityLookup,
    WorkspaceCleanupFilter,
    WorkspaceCleanupResult,
    WorkspaceStatusLookup,
    cleanup_workspace_cache,
    parse_duration_seconds,
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
    repository_id,
)
from .run.workspace.source_types import WorkspacePolicyBuilder

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
            console,
            config_file,
            successful,
            failed,
            cancelled,
            all_projects,
            run_key_name,
            older_than,
            orphans,
        )
    except Exception as exc:
        console.print(f"[red]Cleanup failed:[/] {exc}")
        raise typer.Exit(code=1) from exc

    destructive = yes and not dry_run
    warn_all_projects_cleanup(console, context.all_projects)

    result = execute_workspace_cleanup(context, destructive)
    write_cleanup_result(console, result, destructive)


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
    return cleanup_workspace_cache(
        context.cache_root,
        workspace_cleanup_filter(context),
        dry_run=not destructive,
        status_lookup=cleanup_status_lookup(
            context.project_root,
            context.all_projects,
        ),
        ref_cleanup=workspace_ref_cleanup(
            context.project_root,
            context.all_projects,
        ),
        eligibility_lookup=workspace_cleanup_eligibility_lookup(
            context.project_root,
            context.all_projects,
            context.orphans,
        ),
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


def workspace_cleanup_eligibility_lookup(
    project_root: Path,
    all_projects: bool,
    orphan_cleanup_requested: bool,
) -> WorkspaceCleanupEligibilityLookup:
    """Return a per-entry eligibility callback for workspace cleanup candidates."""
    if all_projects:
        # Cross-project cleanup intentionally skips workspace-specific retention checks.
        def all_projects_lookup(
            _run_key_name: str,  # noqa: ARG001
            _cache_key: str,  # noqa: ARG001
            _status: str | None,  # noqa: ARG001
        ) -> WorkspaceCleanupEligibility:
            return WorkspaceCleanupEligibility(deletable=True)

        return all_projects_lookup

    # Scoped cleanup needs filesystem and process context to verify run ownership/state.
    state_dir = project_root / STATE_DIR_NAME
    inspector = ProcessInspector()

    def lookup(
        run_key_name: str,
        _cache_key: str,  # noqa: ARG001
        status: str | None,
    ) -> WorkspaceCleanupEligibility:
        # Keep blocker logic in one place and treat an explicit block as non-deletable.
        retention_blocker = workspace_cleanup_blocker(
            run_key_name,
            status,
            state_dir,
            orphan_cleanup_requested,
            inspector,
        )
        if retention_blocker is not None:
            return retention_blocker
        return WorkspaceCleanupEligibility(deletable=True)

    return lookup


def workspace_cleanup_blocker(
    run_key_name: str,
    status: str | None,
    state_dir: Path,
    orphan_cleanup_requested: bool,
    inspector: ProcessInspector,
) -> WorkspaceCleanupEligibility | None:
    blocker_reason = _first_cleanup_blocker_reason(
        run_key_name,
        status,
        state_dir,
        orphan_cleanup_requested,
        inspector,
    )
    if blocker_reason is not None:
        return WorkspaceCleanupEligibility(deletable=False, reason=blocker_reason)

    return None


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
        repository_id=repository_id(git_context, project_root),
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
    if cache_root.exists() and cache_root.is_symlink():
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


def cleanup_status_lookup(
    project_root: Path,
    all_projects: bool,
) -> WorkspaceStatusLookup:
    if all_projects:
        return unknown_workspace_status_lookup
    return workspace_status_lookup(project_root)


def unknown_workspace_status_lookup(
    run_key_name: str,  # noqa: ARG001 - Required by WorkspaceStatusLookup.
    cache_key: str,  # noqa: ARG001 - Required by WorkspaceStatusLookup.
) -> str:
    return "unknown"


def workspace_status_lookup(project_root: Path) -> WorkspaceStatusLookup:
    stage_root = project_root / ".crewplane" / "execution-stages"
    cache: dict[tuple[str, str], str] | None = None

    def lookup(run_key_name: str, cache_key: str) -> str | None:
        nonlocal cache
        if cache is None:
            cache = load_workspace_statuses(stage_root)
        return cache.get((run_key_name, cache_key))

    return lookup


def load_workspace_statuses(stage_root: Path) -> dict[tuple[str, str], str]:
    statuses: dict[tuple[str, str], str] = {}
    if not stage_root.is_dir():
        return statuses
    for state_path in _iter_workspace_state_paths(stage_root):
        payload = _parse_workspace_state_payload(state_path)
        if payload is None:
            continue

        entry = _extract_workspace_status(payload)
        if entry is None:
            continue

        run_key_name, cache_key, status = entry
        statuses[(run_key_name, cache_key)] = status
    return statuses


def _iter_workspace_state_paths(stage_root: Path) -> Iterator[Path]:
    return stage_root.glob("*/**/workspace-state*.json")


def _parse_workspace_state_payload(state_path: Path) -> dict[str, object] | None:
    if not state_path.is_file() or state_path.is_symlink():
        return None
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None

    if not isinstance(payload, dict):
        return None

    return payload


def _extract_workspace_status(
    payload: dict[str, object],
) -> tuple[str, str, str] | None:
    run_key_name = payload.get("run_key_name")
    status = payload.get("status")
    workspace = payload.get("workspace")

    if not (
        isinstance(run_key_name, str)
        and isinstance(status, str)
        and isinstance(workspace, dict)
    ):
        return None

    cache_key = workspace.get("cache_key")
    if not isinstance(cache_key, str):
        return None

    return run_key_name, cache_key, status
