from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from rich.console import Console

from orchestrator_cli.architecture.contracts import JsonObject
from orchestrator_cli.architecture.loader import instantiate_adapter
from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.architecture.ports.runtime import RuntimeComponents, UIRuntimePlan
from orchestrator_cli.core.config import Config, Settings
from orchestrator_cli.observability.types import WorkflowTopology


def build_runtime_components(
    config: Config,
    workflow_topology: WorkflowTopology,
    orchestrator_dir: Path,
    project_root: Path,
    console: Console,
    no_live: bool,
    warning_sink: Callable[[str], None] | None = None,
    which_fn: Callable[[str], str | None] | None = None,
    invoker_options: JsonObject | None = None,
    artifact_options: JsonObject | None = None,
    ui_options: JsonObject | None = None,
    artifact_store: ArtifactStorePort | None = None,
) -> RuntimeComponents:
    """Build the concrete runtime components for a workflow execution."""

    settings = config.settings if config.settings is not None else Settings()

    artifacts_spec = settings.integrations.artifacts
    invoker_spec = settings.integrations.invoker
    ui_spec = settings.integrations.ui
    live_ui_enabled = console.is_terminal and not no_live

    invoker_adapter = instantiate_adapter("invoker", invoker_spec.implementation)
    ui_adapter = (
        instantiate_adapter("ui", ui_spec.implementation) if live_ui_enabled else None
    )

    base_invoker = invoker_adapter.create_invoker(
        config=config,
        options=invoker_options
        if invoker_options is not None
        else dict(invoker_spec.options),
    )
    _validate_invoker_contract(base_invoker)
    if artifact_store is None:
        artifacts_adapter = instantiate_adapter(
            "artifacts",
            artifacts_spec.implementation,
        )
        selected_artifact_store = artifacts_adapter.create_store(
            workflow_name=workflow_topology.workflow_name,
            orchestrator_dir=orchestrator_dir,
            project_root=project_root,
            options=(
                artifact_options
                if artifact_options is not None
                else dict(artifacts_spec.options)
            ),
        )
    else:
        selected_artifact_store = artifact_store

    ui_runtime = UIRuntimePlan(
        observers=(),
        suppress_progress_output=False,
    )

    if ui_adapter is not None:
        ui_capabilities = ui_adapter.capabilities
        if (
            ui_capabilities.requires_cli_output_logs
            and not selected_artifact_store.log_cli_output
        ):
            message = (
                "tmux live dashboard requires artifacts option "
                "'log_cli_output=true'; continuing without live dashboard."
            )
            if warning_sink is not None:
                warning_sink(message)
            else:
                console.print(f"[yellow]WARN[/] {message}")
        else:
            ui_runtime = ui_adapter.create_runtime(
                config=config,
                workflow_topology=workflow_topology,
                run_id=selected_artifact_store.run_id,
                console=console,
                options=ui_options if ui_options is not None else dict(ui_spec.options),
                warning_sink=warning_sink,
                which_fn=(which_fn if ui_capabilities.accepts_which_override else None),
            )

    return RuntimeComponents(
        artifact_store=selected_artifact_store,
        base_invoker=base_invoker,
        observers=ui_runtime.observers,
        suppress_progress_output=ui_runtime.suppress_progress_output,
    )


def _validate_invoker_contract(invoker: object) -> None:
    if not callable(getattr(invoker, "invoke", None)):
        raise TypeError("invoker adapter returned object without callable invoke")
    if not callable(getattr(invoker, "log_presentation_for", None)):
        raise TypeError(
            "invoker adapter returned object without callable log_presentation_for"
        )
