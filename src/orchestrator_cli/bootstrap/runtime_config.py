from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console

from orchestrator_cli.architecture.contracts import JsonObject
from orchestrator_cli.architecture.loader import (
    instantiate_adapter,
    resolve_implementation_path,
)
from orchestrator_cli.core.config import Config, Settings
from orchestrator_cli.core.preflight.runtime_config import (
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
