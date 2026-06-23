from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from rich.console import Console

from crewplane.architecture.contracts import (
    CanonicalIntegrationConfig,
    JsonObject,
    WorkflowTopology,
)
from crewplane.architecture.ports.runtime import UIRuntimePlan
from crewplane.core.config import Config


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
        options: JsonObject | None = None,
    ) -> CanonicalIntegrationConfig:
        """Validate and canonicalize UI options without side effects."""

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
        """Build optional observer-only UI components for one run."""
