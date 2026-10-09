from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from crewplane.core.config import AgentConfig, Config
from crewplane.core.preflight.execution_nodes import resolve_provider_model
from crewplane.core.workflow.models import WorkflowPlan

type LauncherKind = Literal["native", "batch", "powershell"]


@dataclass(frozen=True)
class ResolvedCommand:
    """Immutable launcher metadata; construction performs no path validation.

    Attributes:
        executable: Selected executable path, or an unvalidated POSIX lookup result.
        kind: Native execution, batch execution, or PowerShell execution.
        shell: Selected shell path for Windows batch/PowerShell launchers, otherwise
            None. Callers constructing metadata directly must supply required shells.
    """

    executable: str
    kind: LauncherKind = "native"
    shell: str | None = None


def contains_path_separator(value: str) -> bool:
    """Return whether value contains / or backslash, regardless of host platform."""
    return "/" in value or "\\" in value


@dataclass(frozen=True, slots=True)
class RequestValidationTarget:
    """Configured workflow provider requiring request validation."""

    location: str
    agent_config: AgentConfig
    requested_reasoning: str | None
    model: str | None


def request_validation_targets(
    workflow: WorkflowPlan,
    config: Config,
) -> Iterator[RequestValidationTarget]:
    for node in workflow.nodes:
        for provider in node.providers:
            agent_config = config.agents.get(provider.provider)
            if agent_config is None:
                continue
            yield RequestValidationTarget(
                location=(
                    f"workflow '{workflow.name}' -> node '{node.id}' -> provider "
                    f"'{provider.provider}'"
                ),
                agent_config=agent_config,
                requested_reasoning=provider.reasoning,
                model=resolve_provider_model(provider, agent_config),
            )


@dataclass(frozen=True)
class CommandAvailability:
    available: bool
    diagnostic: str | None = None
