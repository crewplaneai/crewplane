from __future__ import annotations

from crewplane.core.config import AgentConfig

from .rendering_config_agents import (
    comment_mock_agent,
    extract_commented_provider_block,
    uncomment_provider_agent,
)
from .rendering_config_invoker import (
    activate_cli_invoker,
    manual_cli_invoker_snippet,
)
from .rendering_config_plugins import activate_plugin_registration, render_local_agent
from .rendering_config_validation import validate_provider_ready_config
from .rendering_errors import OnboardingRenderingError
from .rendering_providers import validate_known_providers
from .rendering_yaml_blocks import uncomment_comment_block


def render_provider_ready_config(
    default_config: str,
    providers: tuple[str, ...],
    local_agent: AgentConfig | None = None,
) -> str:
    validate_known_providers(providers)
    text = comment_mock_agent(default_config)
    for provider in providers:
        if provider == "local":
            if local_agent is None:
                raise OnboardingRenderingError(
                    "Select an Ollama endpoint and model before rendering local."
                )
            text = render_local_agent(text, local_agent)
        else:
            text = uncomment_provider_agent(text, provider)
    text = activate_cli_invoker(text)
    if "local" in providers:
        text = activate_plugin_registration(text)
    validate_provider_ready_config(text, providers)
    return text


def manual_config_snippet(default_config: str, providers: tuple[str, ...]) -> str:
    validate_known_providers(providers)
    agent_blocks = [
        uncomment_comment_block(
            extract_commented_provider_block(default_config, provider)
        ).strip()
        for provider in providers
    ]
    invoker = manual_cli_invoker_snippet(default_config)
    if "local" in providers:
        invoker = activate_plugin_registration(invoker)
    return "\n\n".join([*agent_blocks, invoker])


__all__ = [
    "manual_config_snippet",
    "render_provider_ready_config",
]
