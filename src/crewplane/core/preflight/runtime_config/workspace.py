from __future__ import annotations

from collections.abc import Mapping

from crewplane.architecture.contracts import JsonObject


def invoker_workspace_descriptor(snapshot: object) -> JsonObject | None:
    payload = runtime_snapshot_payload(snapshot)
    invoker = payload.get("invoker") if payload is not None else None
    if not isinstance(invoker, Mapping):
        return None
    capabilities = invoker.get("capabilities")
    workspace = (
        capabilities.get("workspace") if isinstance(capabilities, Mapping) else None
    )
    if not isinstance(workspace, Mapping):
        return None
    descriptor: JsonObject = {
        "implementation": string_value(invoker.get("implementation")),
        "honors_cwd": bool_value(workspace.get("honors_cwd")),
        "launch_mode": string_value(workspace.get("launch_mode")),
        "controlled_child_environment": bool_value(
            workspace.get("controlled_child_environment")
        ),
    }
    return descriptor


def requires_controlled_child_environment(invoker: Mapping[str, object]) -> bool:
    return (
        invoker.get("launch_mode") == "runtime_command_runner"
        and invoker.get("controlled_child_environment") is True
    )


def runtime_snapshot_payload(snapshot: object) -> Mapping[str, object] | None:
    if snapshot is None:
        return None
    if isinstance(snapshot, Mapping):
        return snapshot
    model_dump = getattr(snapshot, "model_dump", None)
    if callable(model_dump):
        payload = model_dump(mode="json")
        return payload if isinstance(payload, Mapping) else None
    return None


def string_value(value: object) -> str | None:
    return value if isinstance(value, str) else None


def bool_value(value: object) -> bool | None:
    return value if isinstance(value, bool) else None
