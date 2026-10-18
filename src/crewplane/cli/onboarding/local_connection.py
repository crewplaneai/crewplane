from __future__ import annotations

import os
from collections.abc import Callable
from importlib import metadata
from pathlib import Path
from stat import S_IMODE
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING

from yaml import YAMLError

from crewplane.adapters.invokers.cli_invoker import (
    CliDiscoveryRequest,
    unconfirmed_process_cleanup,
)
from crewplane.adapters.invokers.cli_invoker.environment import (
    environment_executable_lookup,
)
from crewplane.core.config import AgentConfig

from ..run.preflight import uses_cli_invoker
from . import messages
from .constants import CONFIG_RELATIVE_PATH
from .plugin_setup import LocalSetupFailure, prepare_local_agent
from .rendering_errors import OnboardingRenderingError
from .rendering_local_connection import render_local_connection
from .rendering_yaml_loading import load_config_mapping

if TYPE_CHECKING:
    from .runner import OnboardingOptions


def configure_local_connection(
    options: OnboardingOptions,
    prompt: Callable[[str], str],
    confirm: Callable[[str, bool], bool],
) -> bool:
    """Offer connection setup without requiring or changing a workflow."""
    try:
        metadata.version("crewplane-ollama")
    except metadata.PackageNotFoundError:
        return False
    if not confirm("Configure the Ollama connection? [y/N]: ", False):
        return False

    path = options.project_root / CONFIG_RELATIVE_PATH
    try:
        original = path.read_bytes()
        config = load_config_mapping(original.decode("utf-8"), "existing config")
        if not uses_cli_invoker(config):
            raise OnboardingRenderingError(
                "The active invoker changed; run onboarding again."
            )
        existing = config.agents.get("local")
        if existing is not None and existing.provider_kind != "ollama":
            raise OnboardingRenderingError(
                "Agent 'local' already belongs to another provider."
            )
        agent = prepare_local_agent(
            CliDiscoveryRequest(
                options.project_root,
                os.environ.copy(),
                connection_executable_lookup(options, existing),
                options.discovery_timeout_seconds,
            ),
            options.console,
            prompt,
            existing,
        )
        if not isinstance(agent, AgentConfig):
            messages.print_ollama_unconfigured(
                options.console, agent == LocalSetupFailure.CANCELLED
            )
            return True
        updated = render_local_connection(original.decode("utf-8"), agent)
        if not confirm("Save the Ollama connection in config.yml? [Y/n]: ", True):
            messages.print_no_files_changed(options.console)
            return True
        save_local_connection(path, original, updated, options.write_text)
    except (OSError, ValueError, YAMLError) as exc:
        if unconfirmed_process_cleanup(exc) is not None:
            raise
        messages.print_provider_diagnostic(options.console, "local", str(exc))
        messages.print_ollama_unconfigured(options.console)
        return True
    options.console.print(
        "Ollama connection saved. Run crewplane validate with your workflow to check its model."
    )
    return True


def connection_executable_lookup(
    options: OnboardingOptions, existing: AgentConfig | None
) -> Callable[[str], str | None]:
    if existing is None:
        return options.which_fn
    configured = existing.cli_cmd[0]
    if "/" in configured or "\\" in configured:
        configured = str(options.project_root / configured)
    lookup = environment_executable_lookup(
        existing.environment, options.project_root, options.which_fn
    )

    def find_executable(command: str) -> str | None:
        if command == ("ollama.exe" if os.name == "nt" else "ollama"):
            return lookup(configured) or options.which_fn(command)
        return options.which_fn(command)

    return find_executable


def save_local_connection(
    path: Path,
    original: bytes,
    updated: str,
    write_text: Callable[[Path, str], None],
) -> None:
    path = path.resolve(strict=True)
    with NamedTemporaryFile(
        dir=path.parent, prefix=".ollama-", delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        write_text(temporary_path, updated)
        temporary_path.chmod(S_IMODE(path.stat().st_mode))
        if path.read_bytes() != original:
            raise OnboardingRenderingError(
                "Config changed during Ollama setup; run onboarding again."
            )
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)
