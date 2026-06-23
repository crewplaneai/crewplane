from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from crewplane.architecture.contracts import (
    ChildProcessEnvironment,
    InvocationContext,
)
from crewplane.core.workspace.git_policy import (
    workspace_git_base_environment,
    workspace_git_environment_unset_keys,
)


def prepare_workspace_child_environment(
    invocation_context: InvocationContext | None,
    child_environment: ChildProcessEnvironment | None,
) -> tuple[InvocationContext | None, ChildProcessEnvironment | None]:
    workspace = invocation_context.workspace if invocation_context is not None else None
    if workspace is None:
        return invocation_context, child_environment
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
