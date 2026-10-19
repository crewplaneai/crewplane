"""Interactive local-agent setup via the selected official Ollama plugin.

Discovery callbacks own their read-only checks and cleanup. This module runs
them and collects choices; callers own persistence of the returned settings.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from enum import StrEnum
from functools import partial

from rich.console import Console

from crewplane.adapters.invokers.cli_invoker import (
    CliDiscoveredEndpoint,
    CliDiscoveredModels,
    CliDiscoveryRequest,
    CliProviderDiscovery,
    CliProviderOnboarding,
    unconfirmed_process_cleanup,
)
from crewplane.adapters.invokers.cli_invoker.provider_registrations import (
    canonicalize_provider_options,
    verify_provider_options,
)
from crewplane.core.config import AgentConfig

from . import messages
from .plugin_profile import ollama_provider_options

_LOCAL_AGENT_INVOCATION_TIMEOUT_SECONDS = 900


class LocalSetupFailure(StrEnum):
    """Non-success results: unavailable setup or a selection cancelled with 0."""

    UNAVAILABLE = "unavailable"
    CANCELLED = "cancelled"


def prepare_local_agent(
    request: CliDiscoveryRequest,
    console: Console,
    prompt: Callable[[str], str],
    existing: AgentConfig | None = None,
) -> AgentConfig | LocalSetupFailure:
    """Prepare local settings via the official Ollama recipe without saving them.

    Callbacks run sequentially in separate event loops, each closed before
    diagnostics and prompts. Existing agents skip model discovery and are
    copied with only the executable and environment updated. Command arguments
    and other settings are preserved; endpoint environment values take precedence.

    ValueError and OSError produce a diagnostic and UNAVAILABLE unless they
    carry unconfirmed cleanup evidence, in which case they propagate. All other
    exceptions propagate, including prompt interruptions.

    Returns:
        A prepared agent, CANCELLED for a cancelled selection, or UNAVAILABLE
        for missing discovery results or a reported ValueError/OSError.
    """
    console.print("Inspecting Ollama clients and servers (read-only checks)...")
    try:
        onboarding = _load_ollama_onboarding()
        discovery = run_discovery_callback(partial(onboarding.discover, request))
        endpoint = _select_endpoint(discovery, console, prompt)
        if isinstance(endpoint, LocalSetupFailure):
            return endpoint
        if existing is not None:
            return _refresh_local_agent(existing, discovery.command[0], endpoint)
        available = run_discovery_callback(
            partial(onboarding.list_models, request, endpoint)
        )
        return _create_local_agent(
            discovery.command, endpoint, available, console, prompt
        )
    except (ValueError, OSError) as exc:
        if unconfirmed_process_cleanup(exc) is not None:
            raise
        messages.print_provider_diagnostic(console, "local", str(exc))
        return LocalSetupFailure.UNAVAILABLE


def _load_ollama_onboarding() -> CliProviderOnboarding:
    providers = verify_provider_options(
        canonicalize_provider_options(ollama_provider_options())
    )
    onboarding = providers["ollama"].onboarding
    if onboarding is None:
        raise ValueError(
            "Installed crewplane-ollama lacks onboarding support; upgrade it in Crewplane's Python environment."
        )
    return onboarding


def _select_endpoint(
    discovery: CliProviderDiscovery, console: Console, prompt: Callable[[str], str]
) -> CliDiscoveredEndpoint | LocalSetupFailure:
    for diagnostic in discovery.diagnostics:
        messages.print_provider_diagnostic(console, "local", diagnostic)
    if not discovery.command or not discovery.endpoints:
        return LocalSetupFailure.UNAVAILABLE
    console.print(f"Ollama client: {discovery.command[0]}", markup=False)
    index = select_item(
        "Ollama server",
        tuple(endpoint.label for endpoint in discovery.endpoints),
        console,
        prompt,
    )
    if index is None:
        return LocalSetupFailure.CANCELLED
    endpoint = discovery.endpoints[index]
    console.print(f"Selected endpoint: {endpoint.label}", markup=False)
    return endpoint


def _refresh_local_agent(
    existing: AgentConfig, executable: str, endpoint: CliDiscoveredEndpoint
) -> AgentConfig:
    return existing.model_copy(
        update={
            "cli_cmd": [executable, *existing.cli_cmd[1:]],
            "environment": {**existing.environment, **endpoint.environment},
        }
    )


def _create_local_agent(
    command: tuple[str, ...],
    endpoint: CliDiscoveredEndpoint,
    available: CliDiscoveredModels,
    console: Console,
    prompt: Callable[[str], str],
) -> AgentConfig | LocalSetupFailure:
    for diagnostic in available.diagnostics:
        messages.print_provider_diagnostic(console, "local", diagnostic)
    if not available.models:
        return LocalSetupFailure.UNAVAILABLE
    index = select_item("installed local text model", available.models, console, prompt)
    if index is None:
        return LocalSetupFailure.CANCELLED
    return AgentConfig(
        cli_cmd=list(command),
        provider_kind="ollama",
        default_model=available.models[index],
        environment=dict(endpoint.environment),
        invocation_timeout_seconds=_LOCAL_AGENT_INVOCATION_TIMEOUT_SECONDS,
    )


def run_discovery_callback[T](callback: Callable[[], Awaitable[T]]) -> T:
    """Run a callback in a fresh event loop and return its awaited result.

    Call from synchronous code without a running event loop in this thread.
    The callback is invoked inside the loop; asyncio.run owns shutdown,
    including cancellation of pending tasks, and closes it before returning.
    Exceptions are not caught here.
    """

    async def invoke() -> T:
        return await callback()

    return asyncio.run(invoke())


def select_item(
    label: str, values: tuple[str, ...], console: Console, prompt: Callable[[str], str]
) -> int | None:
    """Return a zero-based selection index, or None when the user enters 0.

    Display values in order as literal text with one-based numbers. Strip
    surrounding input whitespace and repeat invalid input until a listed
    number or 0 is entered, even for a single item. Prompt and console
    exceptions propagate.
    """
    console.print(f"Choose {label}:")
    for index, value in enumerate(values, 1):
        console.print(f"  [{index}] {value}", markup=False)
    while True:
        answer = prompt("Choose a number (0 cancels): ").strip()
        if answer == "0":
            return None
        if answer in {str(index) for index in range(1, len(values) + 1)}:
            return int(answer) - 1
        console.print("Choose one of the listed numbers.")
