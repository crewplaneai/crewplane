from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from orchestrator_cli.architecture.contracts import (
    ChildProcessEnvironment,
    InvocationContext,
)
from orchestrator_cli.core.workspace_git_policy import (
    workspace_git_config_environment,
)

WORKSPACE_GIT_ENV_UNSET = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_ATTR_NOSYSTEM",
    "GIT_ATTR_SOURCE",
    "GIT_LITERAL_PATHSPECS",
    "GIT_GLOB_PATHSPECS",
    "GIT_NOGLOB_PATHSPECS",
    "GIT_ICASE_PATHSPECS",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
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
    unset = (*WORKSPACE_GIT_ENV_UNSET, *dynamic_git_config_environment_keys())
    return ChildProcessEnvironment(
        set={
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CEILING_DIRECTORIES": discovery_root.resolve(
                strict=False
            ).parent.as_posix(),
            **workspace_git_config_environment(),
        },
        unset=unset,
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


def dynamic_git_config_environment_keys() -> tuple[str, ...]:
    return tuple(
        key
        for key in os.environ
        if key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
    )
