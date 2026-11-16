"""Assemble runtime integrations while preserving construction and policy order."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from rich.console import Console

from crewplane.adapters.invokers.mock_invoker.options import bind_project_options
from crewplane.architecture.contracts import JsonObject
from crewplane.architecture.loader import (
    instantiate_adapter,
    is_builtin_implementation,
    require_artifact_store,
    resolve_implementation_path,
)
from crewplane.architecture.ports import (
    ArtifactStorePort,
    InvokerAdapterPort,
    UIAdapterPort,
)
from crewplane.architecture.ports.runtime import RuntimeComponents, UIRuntimePlan
from crewplane.core.config import Config, IntegrationSpec
from crewplane.observability.types import WorkflowTopology


def build_runtime_components(
    config: Config,
    workflow_topology: WorkflowTopology,
    state_dir: Path,
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
    """Assemble an invoker, artifact store, and optional live observer plan.

    Without ``invoker_options``, copy configured options, bind built-in mock
    fixture paths to ``project_root``, and canonicalize through the adapter.
    Supplied options pass directly to invoker construction without those steps.

    Load the invoker and eligible UI adapters before preparing the invoker.
    Create and validate the invoker before selecting the artifact store. Without
    ``artifact_store``, create a store using ``artifact_options`` or a copy of
    configured options; the default filesystem store reserves run state here.
    A supplied store is validated and reused without loading the artifacts
    adapter, and ``artifact_options`` is ignored.

    Plan live UI only for a terminal when ``no_live`` is false. Missing required
    CLI logs emit a warning and skip UI construction, including UI options.
    Otherwise, use ``ui_options`` or a copy of configured options. Return
    observers without starting them. Errors propagate without rolling back
    an already allocated store.
    """

    settings = config.settings

    artifacts_spec = settings.integrations.artifacts
    invoker_spec = settings.integrations.invoker
    ui_spec = settings.integrations.ui
    live_ui_enabled = console.is_terminal and not no_live

    invoker_adapter = instantiate_adapter("invoker", invoker_spec.implementation)
    ui_adapter = (
        instantiate_adapter("ui", ui_spec.implementation) if live_ui_enabled else None
    )

    invoker_options = _prepare_invoker_options(
        invoker_adapter, invoker_spec, project_root, invoker_options
    )
    base_invoker = invoker_adapter.create_invoker(
        config=config,
        options=invoker_options,
    )
    _validate_invoker_contract(base_invoker)
    if artifact_store is None:
        artifacts_adapter = instantiate_adapter(
            "artifacts",
            artifacts_spec.implementation,
        )
        selected_artifact_store = artifacts_adapter.create_store(
            workflow_name=workflow_topology.workflow_name,
            state_dir=state_dir,
            project_root=project_root,
            options=(
                artifact_options
                if artifact_options is not None
                else dict(artifacts_spec.options)
            ),
        )
    else:
        selected_artifact_store = artifact_store

    resolved_artifact_store = require_artifact_store(selected_artifact_store)

    ui_runtime = _plan_ui_runtime(
        ui_adapter,
        ui_spec,
        config,
        workflow_topology,
        resolved_artifact_store,
        console,
        ui_options,
        warning_sink,
        which_fn,
    )

    return RuntimeComponents(
        artifact_store=resolved_artifact_store,
        base_invoker=base_invoker,
        observers=ui_runtime.observers,
        suppress_progress_output=ui_runtime.suppress_progress_output,
    )


def _prepare_invoker_options(
    invoker_adapter: InvokerAdapterPort,
    invoker_spec: IntegrationSpec,
    project_root: Path,
    options: JsonObject | None,
) -> JsonObject:
    if options is not None:
        return options

    authored_options = dict(invoker_spec.options)
    if is_builtin_implementation("invoker", invoker_spec.implementation, "mock"):
        authored_options = bind_project_options(authored_options, project_root)
    return invoker_adapter.canonicalize_options(
        invoker_spec.implementation,
        resolve_implementation_path("invoker", invoker_spec.implementation),
        authored_options,
    ).options


def _plan_ui_runtime(
    ui_adapter: UIAdapterPort | None,
    ui_spec: IntegrationSpec,
    config: Config,
    workflow_topology: WorkflowTopology,
    artifact_store: ArtifactStorePort,
    console: Console,
    options: JsonObject | None,
    warning_sink: Callable[[str], None] | None,
    which_fn: Callable[[str], str | None] | None,
) -> UIRuntimePlan:
    ui_runtime = UIRuntimePlan(observers=(), suppress_progress_output=False)
    if ui_adapter is None:
        return ui_runtime

    ui_capabilities = ui_adapter.capabilities
    if ui_capabilities.requires_cli_output_logs and not artifact_store.log_cli_output:
        message = (
            "tmux live dashboard requires artifacts option "
            "'log_cli_output=true'; continuing without live dashboard."
        )
        if warning_sink is not None:
            warning_sink(message)
        else:
            console.print(f"[yellow]WARN[/] {message}")
        return ui_runtime

    return ui_adapter.create_runtime(
        config=config,
        workflow_topology=workflow_topology,
        run_id=artifact_store.run_id,
        console=console,
        options=options if options is not None else dict(ui_spec.options),
        warning_sink=warning_sink,
        which_fn=which_fn if ui_capabilities.accepts_which_override else None,
    )


def _validate_invoker_contract(invoker: object) -> None:
    for method_name in ("invoke", "log_presentation_for"):
        if not callable(getattr(invoker, method_name, None)):
            raise TypeError(
                f"invoker adapter returned object without callable {method_name}"
            )
