from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from rich.console import Console

from orchestrator_cli.architecture.api_version import EXT_API_VERSION
from orchestrator_cli.architecture.ports import UIAdapterCapabilities
from orchestrator_cli.architecture.ports.runtime import UIRuntimePlan
from orchestrator_cli.core.config import Config
from orchestrator_cli.core.preflight.runtime_config import CanonicalIntegrationConfig
from orchestrator_cli.observability.types import WorkflowTopology


class NullUIAdapter:
    """Create a no-op live runtime plan."""

    capabilities = UIAdapterCapabilities()

    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: Mapping[str, Any] | None = None,
    ) -> CanonicalIntegrationConfig:
        if options:
            raise ValueError(
                "none ui implementation does not support options; "
                f"got: {sorted(options)}"
            )
        return CanonicalIntegrationConfig(
            implementation=implementation,
            resolved_identity=resolved_identity,
            api_version=EXT_API_VERSION,
            options={},
            option_scopes={},
        )

    def create_runtime(
        self,
        config: Config,  # noqa: ARG002 - Required by callback or protocol signature.
        workflow_topology: WorkflowTopology,  # noqa: ARG002 - Required by callback or protocol signature.
        run_id: str,  # noqa: ARG002 - Required by callback or protocol signature.
        console: Console,  # noqa: ARG002 - Required by callback or protocol signature.
        options: Mapping[str, Any] | None = None,
        warning_sink: Callable[[str], None] | None = None,  # noqa: ARG002 - Required by callback or protocol signature.
        which_fn: Callable[[str], str | None] | None = None,  # noqa: ARG002 - Required by callback or protocol signature.
    ) -> UIRuntimePlan:
        """Return an empty runtime plan that leaves execution unchanged."""

        self.canonicalize_options("none", self.__class__.__module__, options)
        return UIRuntimePlan(
            observers=(),
            suppress_progress_output=False,
        )
