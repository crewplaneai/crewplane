from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from rich.console import Console

from orchestrator_cli.architecture.ports.runtime import UIRuntimePlan
from orchestrator_cli.core.config import Config
from orchestrator_cli.core.preflight.runtime_config import CanonicalIntegrationConfig
from orchestrator_cli.observability.types import WorkflowTopology


@dataclass(frozen=True)
class UIAdapterCapabilities:
    """Capability hints that let bootstrap wire optional UI dependencies safely."""

    requires_cli_output_logs: bool = False
    accepts_which_override: bool = False


class UIAdapterPort(Protocol):
    """Factory contract for optional live runtime integrations."""

    capabilities: UIAdapterCapabilities

    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: Mapping[str, Any] | None = None,
    ) -> CanonicalIntegrationConfig:
        """Validate and canonicalize UI options without side effects."""

    def create_runtime(
        self,
        config: Config,
        workflow_topology: WorkflowTopology,
        run_id: str,
        console: Console,
        options: Mapping[str, Any] | None = None,
        warning_sink: Callable[[str], None] | None = None,
        which_fn: Callable[[str], str | None] | None = None,
    ) -> UIRuntimePlan:
        """Build optional observer-only UI components for one run."""
