"""Public capability and planning API for the built-in CLI invoker."""

from crewplane.runtime.agent.invocation.command import run_command_once
from crewplane.runtime.agent.process.drain import unconfirmed_process_cleanup

from .capabilities import (
    build_cli_invocation_plan,
    build_cli_log_presentation,
    get_cli_provider_capability,
)
from .capability import CliCommand, CliInvocationRequest, CliProviderCapability
from .discovery import (
    DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
    CliDiscoveredEndpoint,
    CliDiscoveredModels,
    CliDiscoveryRequest,
    CliProviderDiscovery,
    CliProviderOnboarding,
)
from .streaming import extract_strict_stdout

__all__ = [
    "DEFAULT_DISCOVERY_TIMEOUT_SECONDS",
    "CliCommand",
    "CliInvocationRequest",
    "CliProviderCapability",
    "CliDiscoveryRequest",
    "CliDiscoveredEndpoint",
    "CliDiscoveredModels",
    "CliProviderDiscovery",
    "CliProviderOnboarding",
    "build_cli_invocation_plan",
    "build_cli_log_presentation",
    "get_cli_provider_capability",
    "extract_strict_stdout",
    "run_command_once",
    "unconfirmed_process_cleanup",
]
