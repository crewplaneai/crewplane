from __future__ import annotations

import shutil
from collections.abc import Callable

from crewplane.architecture.loader import instantiate_adapter
from crewplane.architecture.ports import ArtifactStorePort
from crewplane.architecture.ports.runtime import RuntimeComponents
from crewplane.bootstrap import (
    RuntimeConfigSnapshotBuildResult,
    build_runtime_components,
)
from crewplane.core.config import Settings
from crewplane.observability.types import WorkflowTopology

from .context import WorkflowRunContext, print_artifact_locations
from .observability import WorkflowWarningRecorder


def build_components_for_run(
    context: WorkflowRunContext,
    snapshot_result: RuntimeConfigSnapshotBuildResult,
    workflow_topology: WorkflowTopology,
    artifact_store: ArtifactStorePort,
    no_live: bool,
    warning_recorder: WorkflowWarningRecorder,
    which_fn: Callable[[str], str | None] | None,
) -> RuntimeComponents:
    return build_runtime_components(
        config=context.config,
        workflow_topology=workflow_topology,
        state_dir=context.state_dir,
        project_root=context.project_root,
        console=context.console,
        no_live=no_live,
        warning_sink=warning_recorder.sink,
        which_fn=shutil.which if which_fn is None else which_fn,
        invoker_options=snapshot_result.invoker_options,
        artifact_options=snapshot_result.artifact_options,
        ui_options=snapshot_result.ui_options,
        artifact_store=artifact_store,
    )


def allocate_run_output(
    context: WorkflowRunContext,
    snapshot_result: RuntimeConfigSnapshotBuildResult,
    warning_recorder: WorkflowWarningRecorder,
) -> ArtifactStorePort:
    settings = (
        context.config.settings if context.config.settings is not None else Settings()
    )
    artifacts_adapter = instantiate_adapter(
        "artifacts",
        settings.integrations.artifacts.implementation,
    )
    output = artifacts_adapter.create_store(
        workflow_name=context.workflow.name,
        state_dir=context.state_dir,
        project_root=context.project_root,
        options=snapshot_result.artifact_options,
    )
    warning_recorder.bind_run_id(output.run_id)
    print_artifact_locations(context.workflow.name, output, context.console)
    return output
