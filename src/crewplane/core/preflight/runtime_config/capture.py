from __future__ import annotations

from crewplane.architecture.contracts import JsonValue, parse_json_pointer
from crewplane.core.config import Config
from crewplane.core.workflow.models import WorkflowPlan
from crewplane.core.workspace.selection import logical_workspace_selections

from ..compile_state import CompileState, PreflightCompileOptions, has_errors
from ..value_fingerprints import (
    backfill_value_fingerprints,
    load_fingerprint_key_if_needed,
)
from . import RuntimeConfigSnapshot
from .redaction import config_value_handle

_WORKSPACE_SETUP_PROFILE_PATH_PREFIX = "workspace.setup_profiles."
_AGENT_CONFIG_PATH_PREFIX = "agents."


def finalize_fingerprints(
    runtime_snapshot: RuntimeConfigSnapshot,
    options: PreflightCompileOptions,
    state: CompileState,
) -> RuntimeConfigSnapshot:
    fingerprinted_snapshot = _fingerprinted_runtime_snapshot(
        runtime_snapshot,
        options,
        state,
    )
    if has_errors(state):
        return fingerprinted_snapshot
    load_fingerprint_key_if_needed(options, state)
    if has_errors(state):
        return fingerprinted_snapshot
    if state.fingerprint_key is not None:
        backfill_value_fingerprints(state)
    _capture_runtime_config_secrets(fingerprinted_snapshot, state)
    return fingerprinted_snapshot


def _capture_runtime_config_secrets(
    runtime_snapshot: RuntimeConfigSnapshot,
    state: CompileState,
) -> None:
    for path in runtime_snapshot.sensitive_config_paths:
        found, value = _raw_runtime_config_value(runtime_snapshot, path)
        if not found:
            continue
        state.secret_context.put_config_value(config_value_handle(path), value)


def runtime_snapshot_with_effective_sensitive_paths(
    workflow: WorkflowPlan,
    config: Config,
    runtime_snapshot: RuntimeConfigSnapshot,
) -> RuntimeConfigSnapshot:
    paths = _effective_sensitive_config_paths(workflow, config, runtime_snapshot)
    if paths == runtime_snapshot.sensitive_config_paths:
        return runtime_snapshot
    return runtime_snapshot.model_copy(update={"sensitive_config_paths": paths})


def _effective_sensitive_config_paths(
    workflow: WorkflowPlan,
    config: Config,
    runtime_snapshot: RuntimeConfigSnapshot,
) -> list[str]:
    setup_profiles = _selected_workspace_setup_profiles(workflow, config)
    agent_names = _selected_agent_names(workflow)
    configured_setup_profiles = _configured_workspace_setup_profiles(runtime_snapshot)
    configured_agent_names = set(runtime_snapshot.raw_agents)
    return [
        path
        for path in runtime_snapshot.sensitive_config_paths
        if _sensitive_path_is_effective(
            path,
            setup_profiles,
            configured_setup_profiles,
            agent_names,
            configured_agent_names,
        )
    ]


def _selected_workspace_setup_profiles(
    workflow: WorkflowPlan,
    config: Config,
) -> set[str]:
    return {
        selection.setup_profile
        for selection in logical_workspace_selections(workflow, config).values()
        if selection.enabled and selection.setup_profile is not None
    }


def _selected_agent_names(workflow: WorkflowPlan) -> set[str]:
    return {
        provider.provider
        for node in workflow.nodes
        if node.mode != "input"
        for provider in node.providers
    }


def _configured_workspace_setup_profiles(
    runtime_snapshot: RuntimeConfigSnapshot,
) -> set[str]:
    profiles = runtime_snapshot.raw_workspace.get("setup_profiles")
    if isinstance(profiles, dict):
        return set(profiles)
    return set(runtime_snapshot.workspace.setup_profiles)


def _sensitive_path_is_effective(
    path: str,
    setup_profiles: set[str],
    configured_setup_profiles: set[str],
    agent_names: set[str],
    configured_agent_names: set[str],
) -> bool:
    if path.startswith(_AGENT_CONFIG_PATH_PREFIX):
        agent_name = _matched_sensitive_path_owner(
            path,
            _AGENT_CONFIG_PATH_PREFIX,
            configured_agent_names,
            ".",
        )
        return agent_name in agent_names
    if path.startswith(_WORKSPACE_SETUP_PROFILE_PATH_PREFIX):
        profile_name = _matched_sensitive_path_owner(
            path,
            _WORKSPACE_SETUP_PROFILE_PATH_PREFIX,
            configured_setup_profiles,
            ".run.",
        )
        return profile_name in setup_profiles
    return True


def _matched_sensitive_path_owner(
    path: str,
    prefix: str,
    owner_names: set[str],
    suffix: str,
) -> str | None:
    return next(
        (
            owner_name
            for owner_name in sorted(owner_names, key=len, reverse=True)
            if path.startswith(f"{prefix}{owner_name}{suffix}")
        ),
        None,
    )


def _raw_runtime_config_value(
    runtime_snapshot: RuntimeConfigSnapshot,
    path: str,
) -> tuple[bool, JsonValue]:
    if path.startswith(_AGENT_CONFIG_PATH_PREFIX):
        return _raw_agent_config_value(runtime_snapshot, path)
    if path.startswith(_WORKSPACE_SETUP_PROFILE_PATH_PREFIX):
        return _raw_workspace_setup_profile_value(runtime_snapshot, path)
    if path.startswith("/"):
        return _raw_integration_config_value(runtime_snapshot, path)

    parts = path.split(".")
    if len(parts) < 2:
        return False, None
    if parts[0] == "workspace":
        return _value_at_path(runtime_snapshot.raw_workspace, parts[1:])
    return False, None


def _raw_integration_config_value(
    runtime_snapshot: RuntimeConfigSnapshot,
    path: str,
) -> tuple[bool, JsonValue]:
    parts = list(parse_json_pointer(path))
    if parts[:3] == ["integrations", "invoker", "options"]:
        integration = runtime_snapshot.raw_invoker
    elif parts[:3] == ["integrations", "artifacts", "options"]:
        integration = runtime_snapshot.raw_artifacts
    elif parts[:3] == ["integrations", "ui", "options"]:
        integration = runtime_snapshot.raw_ui
    else:
        return False, None
    return _value_at_path(integration.options if integration else {}, parts[3:])


def _raw_agent_config_value(
    runtime_snapshot: RuntimeConfigSnapshot,
    path: str,
) -> tuple[bool, JsonValue]:
    agent_name = _matched_sensitive_path_owner(
        path,
        _AGENT_CONFIG_PATH_PREFIX,
        set(runtime_snapshot.raw_agents),
        ".",
    )
    if agent_name is None:
        return False, None
    relative_path = path.removeprefix(f"{_AGENT_CONFIG_PATH_PREFIX}{agent_name}.")
    parts = (
        relative_path.split(".", 1)
        if relative_path.startswith("environment.")
        else relative_path.split(".")
    )
    return _value_at_path(runtime_snapshot.raw_agents.get(agent_name), parts)


def _raw_workspace_setup_profile_value(
    runtime_snapshot: RuntimeConfigSnapshot,
    path: str,
) -> tuple[bool, JsonValue]:
    profiles = runtime_snapshot.raw_workspace.get("setup_profiles")
    if not isinstance(profiles, dict):
        return False, None
    profile_name = _matched_sensitive_path_owner(
        path,
        _WORKSPACE_SETUP_PROFILE_PATH_PREFIX,
        set(profiles),
        ".run.",
    )
    if profile_name is None:
        return False, None
    relative_path = path.removeprefix(
        f"{_WORKSPACE_SETUP_PROFILE_PATH_PREFIX}{profile_name}."
    )
    return _value_at_path(profiles.get(profile_name), relative_path.split("."))


def _value_at_path(
    payload: JsonValue,
    parts: list[str],
) -> tuple[bool, JsonValue]:
    current = payload
    for part in parts:
        if isinstance(current, dict):
            if part not in current:
                return False, None
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                return False, None
            current = current[index]
            continue
        return False, None
    return True, current


def _fingerprinted_runtime_snapshot(
    runtime_snapshot: RuntimeConfigSnapshot,
    options: PreflightCompileOptions,
    state: CompileState,
) -> RuntimeConfigSnapshot:
    if not runtime_snapshot.sensitive_config_paths:
        return runtime_snapshot.with_sensitive_config_fingerprints(None)
    state.sensitive_values_required = True
    load_fingerprint_key_if_needed(options, state)
    if state.fingerprint_key is None:
        return runtime_snapshot
    return runtime_snapshot.with_sensitive_config_fingerprints(state.fingerprint_key)
