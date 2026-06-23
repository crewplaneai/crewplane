from __future__ import annotations

from crewplane.core.config import Config
from crewplane.core.preflight.compile_state import (
    CompileState,
    append_diagnostic,
)
from crewplane.core.preflight.diagnostics import (
    PreflightDiagnosticCode,
    PreflightDiagnosticPhase,
)


def collect_prompt_transport_warnings(
    config: Config,
    state: CompileState,
) -> None:
    for agent_key, agent in config.agents.items():
        if agent.prompt_transport != "argv":
            continue
        append_prompt_transport_warning(agent_key, state)


def append_prompt_transport_warning(
    agent_key: str,
    state: CompileState,
) -> None:
    message = (
        f"Agent '{agent_key}' uses argv prompt transport for rendered prompts. "
        "This may expose prompt contents in process arguments."
    )
    append_diagnostic(
        state,
        code=PreflightDiagnosticCode.PROVIDER_CONFIG,
        phase=PreflightDiagnosticPhase.PROVIDER,
        message=message,
        severity="warning",
    )
