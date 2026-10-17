from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import partial

from rich.console import Console

from crewplane.adapters.invokers.cli_invoker import (
    CliDiscoveryRequest,
    unconfirmed_process_cleanup,
)
from crewplane.adapters.invokers.cli_invoker.provider_registrations import (
    canonicalize_provider_options,
    verify_provider_options,
)
from crewplane.core.config import AgentConfig

from .plugin_profile import ollama_provider_options


def prepare_local_agent(
    request: CliDiscoveryRequest, console: Console, prompt: Callable[[str], str]
) -> AgentConfig | None:
    """Load only the selected official recipe; plugin callbacks own discovery."""
    console.print("Inspecting Ollama clients and servers (read-only checks)...")
    try:
        providers = verify_provider_options(
            canonicalize_provider_options(ollama_provider_options())
        )
        onboarding = providers["ollama"].onboarding
        if onboarding is None:
            raise ValueError(
                "Installed crewplane-ollama lacks onboarding support; upgrade it in Crewplane's Python environment."
            )
        discovery = run_discovery_callback(partial(onboarding.discover, request))
        for diagnostic in discovery.diagnostics:
            console.print(diagnostic, markup=False)
        if not discovery.command or not discovery.endpoints:
            return None
        console.print(f"Ollama client: {discovery.command[0]}", markup=False)
        index = select_item(
            "Ollama server",
            tuple(endpoint.label for endpoint in discovery.endpoints),
            console,
            prompt,
        )
        if index is None:
            return None
        endpoint = discovery.endpoints[index]
        console.print(f"Selected endpoint: {endpoint.label}", markup=False)
        available = run_discovery_callback(
            partial(onboarding.list_models, request, endpoint)
        )
        for diagnostic in available.diagnostics:
            console.print(diagnostic, markup=False)
        if not available.models:
            return None
        index = select_item(
            "installed local text model", available.models, console, prompt
        )
        if index is None:
            return None
        return AgentConfig(
            cli_cmd=list(discovery.command),
            provider_kind="ollama",
            default_model=available.models[index],
            environment=dict(endpoint.environment),
            invocation_timeout_seconds=900,
        )
    except (ValueError, OSError) as exc:
        if unconfirmed_process_cleanup(exc) is not None:
            raise
        console.print(f"Ollama setup failed: {exc}", markup=False)
        return None


def run_discovery_callback[T](callback: Callable[[], Awaitable[T]]) -> T:
    async def invoke() -> T:
        return await callback()

    return asyncio.run(invoke())


def select_item(
    label: str, values: tuple[str, ...], console: Console, prompt: Callable[[str], str]
) -> int | None:
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
