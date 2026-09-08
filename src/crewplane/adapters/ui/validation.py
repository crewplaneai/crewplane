from __future__ import annotations

from rich.console import Console

from crewplane.core.config import Config
from crewplane.observability.types import WorkflowTopology


def validate_runtime_request(
    config: Config,
    workflow_topology: WorkflowTopology,
    run_id: str,
    console: Console,
) -> None:
    if not isinstance(config, Config):
        raise TypeError("config must be a Config instance")
    if not isinstance(workflow_topology, WorkflowTopology):
        raise TypeError("workflow_topology must be a WorkflowTopology instance")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id must be a non-empty string")
    if not isinstance(console, Console):
        raise TypeError("console must be a Console instance")
