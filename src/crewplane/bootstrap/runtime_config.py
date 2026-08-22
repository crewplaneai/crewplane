from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from rich.console import Console

from crewplane.architecture.contracts import (
    CanonicalIntegrationConfig,
    JsonObject,
)
from crewplane.architecture.loader import (
    instantiate_adapter,
    resolve_implementation_path,
)
from crewplane.core.config import Config
from crewplane.core.preflight.runtime_config import (
    RuntimeConfigSnapshot,
    RuntimeConfigSnapshotOptions,
)


@dataclass(frozen=True)
class RuntimeConfigSnapshotBuildResult:
    """Canonical runtime snapshot plus shallow option copies for construction."""

    snapshot: RuntimeConfigSnapshot
    invoker_options: JsonObject = field(repr=False)
    artifact_options: JsonObject = field(repr=False)
    ui_options: JsonObject = field(repr=False)


def build_runtime_config_snapshot(
    config: Config,
    console: Console,
    no_live: bool,
) -> RuntimeConfigSnapshotBuildResult:
    """Resolve adapters and canonicalize options without run side effects."""

    settings = config.settings
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

    invoker_config = _canonicalize_integration_options(
        "invoker",
        invoker_spec.implementation,
        invoker_identity,
        dict(invoker_spec.options),
        invoker_adapter.canonicalize_options,
    )
    artifact_config = _canonicalize_integration_options(
        "artifacts",
        artifacts_spec.implementation,
        artifacts_identity,
        dict(artifacts_spec.options),
        artifacts_adapter.canonicalize_options,
    )
    ui_config = _canonicalize_integration_options(
        "ui",
        ui_spec.implementation,
        ui_identity,
        dict(ui_spec.options),
        ui_adapter.canonicalize_options,
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
        file_access=settings.file_access,
    )
    return RuntimeConfigSnapshotBuildResult(
        snapshot=snapshot,
        invoker_options=dict(invoker_config.options),
        artifact_options=dict(artifact_config.options),
        ui_options=dict(ui_config.options),
    )


def _canonicalize_integration_options(
    integration_kind: str,
    implementation: str,
    resolved_identity: str,
    options: JsonObject,
    canonicalize: Callable[[str, str, JsonObject | None], CanonicalIntegrationConfig],
) -> CanonicalIntegrationConfig:
    try:
        canonical_config = canonicalize(implementation, resolved_identity, options)
    except Exception:
        if implementation != resolved_identity:
            raise
        error_message = (
            f"Failed to canonicalize {integration_kind} integration; "
            "adapter validation failed."
        )
    else:
        return canonical_config
    raise ValueError(error_message)
