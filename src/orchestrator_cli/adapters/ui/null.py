from __future__ import annotations

from collections.abc import Callable

from rich.console import Console

from orchestrator_cli.architecture.contracts import (
    CanonicalIntegrationConfig,
    JsonObject,
)
from orchestrator_cli.architecture.ports import UIAdapterCapabilities
from orchestrator_cli.architecture.ports.runtime import UIRuntimePlan
from orchestrator_cli.core.config import Config
from orchestrator_cli.observability.types import WorkflowTopology
from orchestrator_cli.versions import INTEGRATION_API_VERSION


class NullUIAdapter:
    """Create a no-op live runtime plan."""

    capabilities = UIAdapterCapabilities()

    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: JsonObject | None = None,
    ) -> CanonicalIntegrationConfig:
        if options:
            raise ValueError(
                "none ui implementation does not support options; "
                f"got: {sorted(options)}"
            )
        return CanonicalIntegrationConfig(
            implementation=implementation,
            resolved_identity=resolved_identity,
            api_version=INTEGRATION_API_VERSION,
            options={},
            option_scopes={},
        )

    def create_runtime(
        self,
        config: Config,
        workflow_topology: WorkflowTopology,
        run_id: str,
        console: Console,
        options: JsonObject | None = None,
        warning_sink: Callable[[str], None] | None = None,
        which_fn: Callable[[str], str | None] | None = None,
    ) -> UIRuntimePlan:
        """Return an empty runtime plan that leaves execution unchanged."""

        _validate_runtime_request(
            config,
            workflow_topology,
            run_id,
            console,
            warning_sink,
            which_fn,
        )
        self.canonicalize_options("none", self.__class__.__module__, options)
        return UIRuntimePlan(
            observers=(),
            suppress_progress_output=False,
        )


def _validate_runtime_request(
    config: Config,
    workflow_topology: WorkflowTopology,
    run_id: str,
    console: Console,
    warning_sink: Callable[[str], None] | None,
    which_fn: Callable[[str], str | None] | None,
) -> None:
    if not isinstance(config, Config):
        raise TypeError("config must be a Config instance")
    if not isinstance(workflow_topology, WorkflowTopology):
        raise TypeError("workflow_topology must be a WorkflowTopology instance")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id must be a non-empty string")
    if not isinstance(console, Console):
        raise TypeError("console must be a Console instance")
    if warning_sink is not None and not callable(warning_sink):
        raise TypeError("warning_sink must be callable")
    if which_fn is not None and not callable(which_fn):
        raise TypeError("which_fn must be callable")
