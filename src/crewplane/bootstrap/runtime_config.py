from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from rich.console import Console

from crewplane.architecture.contracts import (
    CanonicalIntegrationConfig,
    InvokerAdapterCapabilities,
    JsonObject,
)
from crewplane.architecture.loader import (
    instantiate_adapter,
    resolve_implementation_path,
)
from crewplane.core.config import Config, Settings
from crewplane.core.preflight.runtime_config import (
    RuntimeConfigSnapshot,
    RuntimeConfigSnapshotOptions,
)


@dataclass(frozen=True)
class RuntimeConfigSnapshotBuildResult:
    snapshot: RuntimeConfigSnapshot
    invoker_options: JsonObject
    artifact_options: JsonObject
    ui_options: JsonObject


def build_runtime_config_snapshot(
    config: Config,
    console: Console,
    no_live: bool,
) -> RuntimeConfigSnapshotBuildResult:
    """Resolve adapters and canonicalize options without run side effects."""

    settings = config.settings if config.settings is not None else Settings()
    invoker_spec = settings.integrations.invoker
    artifacts_spec = settings.integrations.artifacts
    ui_spec = settings.integrations.ui

    invoker_identity = resolve_implementation_path(
        "invoker", invoker_spec.implementation
    )
    artifacts_identity = resolve_implementation_path(
        "artifacts",
        artifacts_spec.implementation,
    )
    ui_identity = resolve_implementation_path("ui", ui_spec.implementation)

    invoker_adapter = instantiate_adapter("invoker", invoker_spec.implementation)
    artifacts_adapter = instantiate_adapter("artifacts", artifacts_spec.implementation)
    ui_adapter = instantiate_adapter("ui", ui_spec.implementation)

    invoker_config = invoker_adapter.canonicalize_options(
        invoker_spec.implementation,
        invoker_identity,
        dict(invoker_spec.options),
    )
    invoker_config = normalize_invoker_capabilities(invoker_adapter, invoker_config)
    artifact_config = artifacts_adapter.canonicalize_options(
        artifacts_spec.implementation,
        artifacts_identity,
        dict(artifacts_spec.options),
    )
    ui_config = ui_adapter.canonicalize_options(
        ui_spec.implementation,
        ui_identity,
        dict(ui_spec.options),
    )
    snapshot = RuntimeConfigSnapshot.build(
        config=config,
        invoker=invoker_config,
        artifacts=artifact_config,
        ui=ui_config,
        options=RuntimeConfigSnapshotOptions(
            no_live=no_live,
            console_is_terminal=console.is_terminal,
        ),
    )
    return RuntimeConfigSnapshotBuildResult(
        snapshot=snapshot,
        invoker_options=dict(invoker_config.options),
        artifact_options=dict(artifact_config.options),
        ui_options=dict(ui_config.options),
    )


def normalize_invoker_capabilities(
    invoker_adapter: object,
    invoker_config: CanonicalIntegrationConfig,
) -> CanonicalIntegrationConfig:
    declared = declared_invoker_capabilities(invoker_adapter)
    capabilities = dict(invoker_config.capabilities)
    if declared is None:
        capabilities.setdefault(
            "workspace",
            InvokerAdapterCapabilities.unsupported().as_dict()["workspace"],
        )
        return invoker_config.model_copy(update={"capabilities": capabilities})
    merge_declared_invoker_capabilities(capabilities, declared)
    return invoker_config.model_copy(update={"capabilities": capabilities})


def declared_invoker_capabilities(invoker_adapter: object) -> JsonObject | None:
    capability_factory = getattr(invoker_adapter, "workspace_capabilities", None)
    if not callable(capability_factory):
        return None
    capabilities = capability_factory()
    as_dict = getattr(capabilities, "as_dict", None)
    if not callable(as_dict):
        raise ValueError(
            "Invoker adapter workspace_capabilities() must return capability metadata."
        )
    payload = as_dict()
    if not isinstance(payload, Mapping):
        raise ValueError(
            "Invoker adapter workspace_capabilities() returned non-mapping metadata."
        )
    return dict(payload)


def merge_declared_invoker_capabilities(
    capabilities: JsonObject,
    declared: JsonObject,
) -> None:
    for name, value in declared.items():
        existing = capabilities.get(name)
        if existing is not None and existing != value:
            raise ValueError(
                "Invoker adapter workspace capability metadata conflicts with "
                f"canonicalized capability '{name}'."
            )
        capabilities[name] = value
