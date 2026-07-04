from __future__ import annotations

from .rendering_config_agents import (
    comment_mock_agent,
    extract_commented_provider_block,
    uncomment_provider_agent,
)
from .rendering_config_invoker import (
    activate_cli_invoker,
    manual_cli_invoker_snippet,
)
from .rendering_config_validation import validate_provider_ready_config
from .rendering_providers import validate_known_provider
from .rendering_yaml_blocks import uncomment_comment_block


def render_provider_ready_config(default_config: str, provider: str) -> str:
    validate_known_provider(provider)
    text = comment_mock_agent(default_config)
    text = uncomment_provider_agent(text, provider)
    text = activate_cli_invoker(text)
    validate_provider_ready_config(text, provider)
    return text


def manual_config_snippet(default_config: str, provider: str) -> str:
    validate_known_provider(provider)
    agent_block = uncomment_comment_block(
        extract_commented_provider_block(default_config, provider)
    ).strip()
    return "\n\n".join((agent_block, manual_cli_invoker_snippet(default_config)))


__all__ = [
    "manual_config_snippet",
    "render_provider_ready_config",
]
