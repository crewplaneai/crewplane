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
    """Offer Ollama connection setup without reading or changing workflows.

    After initial confirmation, OSError, ValueError, and YAMLError are reported
    as handled failures unless they carry unconfirmed process cleanup evidence.
    Other exceptions, including prompt interruptions, propagate.

    Returns:
        False if the plugin is missing or the initial offer is declined. True
        for all handled outcomes after acceptance, including successful saves,
        failed or cancelled setup, and a declined save.
    """
    try:
        metadata.version("crewplane-ollama")
    except metadata.PackageNotFoundError:
        return False
    if not confirm("Configure the Ollama connection? [y/N]: ", False):
        return False

    path = options.project_root / CONFIG_RELATIVE_PATH
    try:
        original = path.read_bytes()
        existing = _load_existing_local_agent(original)
        agent = _prepare_connection_agent(options, prompt, existing)
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


def _load_existing_local_agent(original: bytes) -> AgentConfig | None:
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
    return existing


def _prepare_connection_agent(
    options: OnboardingOptions,
    prompt: Callable[[str], str],
    existing: AgentConfig | None,
) -> AgentConfig | LocalSetupFailure:
    return prepare_local_agent(
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


def connection_executable_lookup(
    options: OnboardingOptions, existing: AgentConfig | None
) -> Callable[[str], str | None]:
    """Build discovery lookup with the configured Ollama executable preferred.

    With no existing agent, return options.which_fn unchanged. Otherwise, look
    up the platform's standard Ollama name using existing.cli_cmd[0] first.
    Resolve command paths and relative agent PATH entries against project_root;
    an agent PATH overrides the fallback lookup for the configured command.
    If that command is unavailable, try options.which_fn for the standard name.
    All other executable names go directly to options.which_fn.
    """
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
    """Stage updated text and replace the config if its bytes still match.

    Resolve symlinks and stage beside the resolved target, keeping the symlinks
    intact and copying the target's current permission bits before replacement.
    The writer must write the supplied text to the supplied temporary path;
    this function owns replacement and temporary-file cleanup. Writer and
    filesystem exceptions propagate.

    Reject changes observed before replacement without overwriting them. This
    optimistic comparison does not lock out concurrent writers. The temporary
    file is removed on success or failure.

    Raises:
        OnboardingRenderingError: The target's bytes no longer match original.
        OSError: Resolving, staging, writing, replacing, or cleanup fails.
    """
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
