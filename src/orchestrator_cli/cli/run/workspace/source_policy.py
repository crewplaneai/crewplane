from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

from orchestrator_cli.core.config import Config, Settings
from orchestrator_cli.core.platform import is_native_windows
from orchestrator_cli.core.preflight.models import WorkspaceSourceSnapshot
from orchestrator_cli.core.workflow_models import WorkflowPlan
from orchestrator_cli.core.workflow_validation_workspace import (
    collect_workspace_policy_diagnostics,
    graph_safe_logical_workspace_selections,
    workflow_has_selected_managed_workspaces,
)
from orchestrator_cli.core.workspace_policy import WorktreeContract

from .cache_policy import validate_cache_root
from .disk_policy import warn_storage_pressure
from .filesystem_policy import probe_filesystem_capabilities
from .git_index import validate_index_extensions
from .git_source import (
    GitSourceContext,
    discover_git_context,
    git_error,
    repository_id,
    validate_git_capabilities,
    validate_git_version,
)
from .repo_policy import (
    inspect_local_git_config,
    run_clean_start,
    validate_clean_start,
    validate_index_flags,
    validate_local_git_config,
    validate_local_policy_files,
    validate_source_tree,
    validate_unsupported_repo_state,
)
from .source_types import WorkspacePolicyBuilder, WorkspacePolicyCheck


def collect_workspace_source_policy(
    config: Config,
    workflow: WorkflowPlan,
    project_root: Path,
    orchestrator_dir: Path,
    real_execution: bool,
    invoker_capabilities: Mapping[str, object] | None = None,
) -> WorkspacePolicyCheck:
    settings = config.settings if config.settings is not None else Settings()
    if not settings.workspace.enabled:
        return WorkspacePolicyCheck()
    if not workflow_has_selected_managed_workspaces(workflow, config):
        return WorkspacePolicyCheck()

    builder = WorkspacePolicyBuilder()
    if is_native_windows():
        builder.errors.append(
            "Workspace-enabled runs are not supported on native Windows. "
            "Use WSL or a POSIX environment."
        )
        return builder.result()
    if has_workspace_policy_errors(workflow, config):
        return builder.result()

    validate_invoker_workspace_support(
        real_execution,
        workflow,
        invoker_capabilities,
        builder,
    )
    validate_workspace_cli_executables(
        config,
        workflow,
        real_execution,
        invoker_capabilities,
        builder,
    )
    if builder.errors:
        return builder.result()
    git_context = discover_git_context(project_root, builder)
    if git_context is None:
        return builder.result()

    validate_git_version(git_context.git_version, builder)
    if builder.errors:
        return builder.result()
    validate_git_capabilities(git_context, builder)
    if builder.errors:
        return builder.result()
    local_config_policy, filesystem_capabilities = collect_git_source_checks(
        settings,
        project_root,
        orchestrator_dir,
        git_context,
        workflow_requires_full_repository_checkout(workflow, config),
        selected_logical_worktree_names(workflow, config),
        real_execution,
        builder,
    )

    return builder.result(
        source_snapshot=workspace_source_snapshot(
            git_context,
            project_root,
            settings,
            local_config_policy,
            filesystem_capabilities,
        )
    )


def collect_git_source_checks(
    settings: Settings,
    project_root: Path,
    orchestrator_dir: Path,
    git_context: GitSourceContext,
    estimate_full_repository: bool,
    logical_worktree_names: tuple[str, ...],
    real_execution: bool,
    builder: WorkspacePolicyBuilder,
) -> tuple[dict[str, tuple[str, ...]], dict[str, bool]]:
    local_config_policy: dict[str, tuple[str, ...]] | None = None
    filesystem_capabilities: dict[str, bool] = {}
    try:
        error_count = len(builder.errors)
        validate_cache_root(
            settings,
            project_root,
            orchestrator_dir,
            git_context,
            builder,
        )
        if len(builder.errors) > error_count:
            return {}, {}
        if real_execution:
            filesystem_capabilities = probe_filesystem_capabilities(settings, builder)
        validate_unsupported_repo_state(project_root, git_context, builder)
        local_config_policy = validate_local_git_config(project_root, builder)
        validate_local_policy_files(git_context, builder)
        validate_index_extensions(git_context, builder)
        validate_index_flags(project_root, builder)
        validate_clean_start(
            project_root,
            settings,
            builder,
            logical_worktree_names,
            git_context,
        )
        validate_source_tree(git_context, builder)
        warn_storage_pressure(settings, git_context, estimate_full_repository, builder)
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        ValueError,
    ) as exc:
        builder.errors.append(
            "Workspace source policy failed: Git source inspection failed "
            f"({git_error(exc)})."
        )
    return local_config_policy or {}, filesystem_capabilities


def workflow_requires_full_repository_checkout(
    workflow: WorkflowPlan,
    config: Config,
) -> bool:
    selections = graph_safe_logical_workspace_selections(workflow, config)
    if selections is None:
        return False
    return any(
        selection.enabled and selection.declaration_kind == "worktree"
        for selection in selections.values()
    )


def selected_logical_worktree_names(
    workflow: WorkflowPlan,
    config: Config,
) -> tuple[str, ...]:
    selections = graph_safe_logical_workspace_selections(workflow, config)
    if selections is None:
        return ()
    return tuple(
        sorted(
            {
                selection.logical_worktree_name
                for selection in selections.values()
                if selection.enabled and selection.logical_worktree_name is not None
            }
        )
    )


def workspace_source_snapshot(
    git_context: GitSourceContext,
    project_root: Path,
    settings: Settings,
    local_config_policy: dict[str, tuple[str, ...]] | None = None,
    filesystem_capabilities: dict[str, bool] | None = None,
) -> WorkspaceSourceSnapshot:
    return WorkspaceSourceSnapshot(
        worktree_contract=WorktreeContract(mode=settings.workspace.worktree_contract),
        run_base_commit=git_context.run_base_commit,
        source_tree=git_context.source_tree,
        object_format=git_context.object_format,
        repository_id=repository_id(git_context, project_root),
        git_version=git_context.git_version,
        git_top_level=git_context.git_top_level.as_posix(),
        project_root_relative_path=git_context.project_root_relative_path,
        active_git_dir=git_context.active_git_dir.as_posix(),
        common_git_dir=git_context.common_git_dir.as_posix(),
        clean_start=run_clean_start(settings),
        local_config_policy=local_config_policy
        if local_config_policy is not None
        else inspect_local_git_config(project_root),
        filesystem_capabilities=filesystem_capabilities or {},
    )


def validate_invoker_workspace_support(
    real_execution: bool,
    workflow: WorkflowPlan,
    invoker_capabilities: Mapping[str, object] | None,
    builder: WorkspacePolicyBuilder,
) -> None:
    if not real_execution or not any(node.providers for node in workflow.nodes):
        return
    if isinstance(invoker_capabilities, Mapping):
        workspace = invoker_capabilities.get("workspace")
    else:
        workspace = None
    if not isinstance(workspace, Mapping) or not workspace.get("supported"):
        builder.errors.append(
            "Workspace invoker compatibility failed: selected invoker does not "
            "declare the workspace launch contract."
        )
        return
    launch_mode = workspace.get("launch_mode")
    honors_cwd = workspace.get("honors_cwd")
    controlled_env = workspace.get("controlled_child_environment")
    if launch_mode == "mock_no_child_process" and honors_cwd:
        return
    if launch_mode == "runtime_command_runner" and honors_cwd and controlled_env:
        return
    builder.errors.append(
        "Workspace invoker compatibility failed: selected invoker cannot honor "
        "the workspace cwd and controlled child-environment launch contract."
    )


def validate_workspace_cli_executables(
    config: Config,
    workflow: WorkflowPlan,
    real_execution: bool,
    invoker_capabilities: Mapping[str, object] | None,
    builder: WorkspacePolicyBuilder,
) -> None:
    if not real_execution or not _uses_runtime_command_runner(invoker_capabilities):
        return
    managed_node_ids = selected_managed_workspace_node_ids(workflow, config)
    provider_names = sorted(
        {
            provider.provider
            for node in workflow.nodes
            if node.id in managed_node_ids
            for provider in node.providers
        }
    )
    for provider_name in provider_names:
        agent = config.agents.get(provider_name)
        if agent is None:
            continue
        executable = agent.cli_cmd[0]
        if _is_relative_path_executable(executable):
            builder.errors.append(
                "Workspace invoker compatibility failed: agent "
                f"'{provider_name}' uses relative path executable '{executable}'. "
                "Workspace runtime_command_runner invocations require an absolute "
                "executable path or a PATH-resolved command name."
            )


def selected_managed_workspace_node_ids(
    workflow: WorkflowPlan,
    config: Config,
) -> set[str]:
    selections = graph_safe_logical_workspace_selections(workflow, config)
    if selections is None:
        return set()
    return {
        node_id
        for node_id, selection in selections.items()
        if selection.enabled and selection.materialization != "project_root"
    }


def _uses_runtime_command_runner(
    invoker_capabilities: Mapping[str, object] | None,
) -> bool:
    if not isinstance(invoker_capabilities, Mapping):
        return False
    workspace = invoker_capabilities.get("workspace")
    return (
        isinstance(workspace, Mapping)
        and workspace.get("launch_mode") == "runtime_command_runner"
    )


def _is_relative_path_executable(executable: str) -> bool:
    return not Path(executable).is_absolute() and (
        "/" in executable or "\\" in executable
    )


def has_workspace_policy_errors(workflow: WorkflowPlan, config: Config) -> bool:
    return any(
        diagnostic.severity == "error"
        for diagnostic in collect_workspace_policy_diagnostics(workflow, config)
    )
