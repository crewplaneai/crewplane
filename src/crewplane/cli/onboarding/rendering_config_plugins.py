from textwrap import indent

import yaml

from crewplane.core.config import AgentConfig

from .plugin_profile import ollama_provider_options
from .rendering_config_agents import extract_commented_provider_block
from .rendering_yaml_blocks import replace_once


def render_local_agent(text: str, agent: AgentConfig) -> str:
    block = extract_commented_provider_block(text, "local")
    replacement = indent(
        yaml.safe_dump(
            {"local": agent.model_dump(mode="json", exclude_defaults=True)},
            sort_keys=False,
        ).rstrip(),
        "  ",
    )
    return replace_once(text, block, replacement + "\n", "local agent example")


def activate_plugin_registration(text: str) -> str:
    replacement = indent(
        yaml.safe_dump(
            {"options": ollama_provider_options()}, sort_keys=False
        ).rstrip(),
        "      ",
    )
    return replace_once(text, "      options: {}", replacement, "CLI provider options")
