from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from crewplane.architecture.contracts import (
    ChildProcessEnvironment,
    InvocationContext,
)
from crewplane.core.agent_environment import merge_agent_environment
from crewplane.core.workspace.git_policy import (
    workspace_git_base_environment,
    workspace_git_environment_unset_keys,
)


def with_agent_environment(
    environment: Mapping[str, str],
    controlled: ChildProcessEnvironment | None,
    managed_workspace: bool = False,
) -> ChildProcessEnvironment | None:
    if not environment:
        return controlled
    if controlled is None:
        return ChildProcessEnvironment(set=environment, unset=())
    values = merge_agent_environment({}, environment)
    unset = tuple(merge_agent_environment({}, dict.fromkeys(controlled.unset, "")))
    if managed_workspace:
        unset = tuple(
            dict.fromkeys((*unset, *workspace_git_environment_unset_keys(values)))
        )
    for name in unset:
        values.pop(name, None)
    values = merge_agent_environment(values, controlled.set)
    return ChildProcessEnvironment(set=values, unset=unset)


def prepare_workspace_child_environment(
    invocation_context: InvocationContext | None,
    child_environment: ChildProcessEnvironment | None,
) -> tuple[InvocationContext | None, ChildProcessEnvironment | None]:
    if invocation_context is None or invocation_context.workspace is None:
        return invocation_context, child_environment
    workspace = invocation_context.workspace
    if not workspace.child_environment_required and child_environment is None:
        return invocation_context, None

    resolved_environment = child_environment or workspace_child_environment(
        workspace.cwd,
        workspace.checkout_root,
    )
    applied_workspace = replace(workspace, child_environment_applied=False)
    return (
        replace(invocation_context, workspace=applied_workspace),
        resolved_environment,
    )


def workspace_child_environment(
    cwd: Path,
    checkout_root: Path | None = None,
) -> ChildProcessEnvironment:
    discovery_root = checkout_root or cwd
    ceiling_directories = discovery_root.resolve(strict=False).parent
    return ChildProcessEnvironment(
        set=workspace_git_base_environment(
            ceiling_directories=ceiling_directories,
            include_config_overlay=True,
        ),
        unset=workspace_git_environment_unset_keys(),
    )


def record_workspace_child_environment_applied(
    invocation_context: InvocationContext | None,
    child_environment: ChildProcessEnvironment | None,
) -> None:
    if child_environment is None or invocation_context is None:
        return
    workspace = invocation_context.workspace
    if workspace is None or not workspace.child_environment_required:
        return
    recorder = invocation_context.workspace_environment_applied_recorder
    if recorder is not None:
        recorder()
