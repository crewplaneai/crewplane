from __future__ import annotations

from textwrap import indent

import yaml
from yaml.nodes import MappingNode, ScalarNode

from crewplane.core.config import AgentConfig

from .plugin_profile import ollama_provider_options
from .rendering_errors import OnboardingRenderingError
from .rendering_yaml_blocks import leading_spaces
from .rendering_yaml_loading import load_config_mapping


def render_local_connection(text: str, agent: AgentConfig) -> str:
    config = load_config_mapping(text, "existing config")
    options = config.settings.integrations.invoker.options
    registrations = options.setdefault("providers", {})
    recipe = ollama_provider_options()["providers"]
    assert isinstance(recipe, dict)
    if not isinstance(registrations, dict):
        raise OnboardingRenderingError("CLI provider registrations must be a mapping.")
    if "ollama" in registrations and registrations["ollama"] != recipe["ollama"]:
        raise OnboardingRenderingError(
            "The existing Ollama plugin registration is customized; leave it unchanged."
        )

    if "local" in config.agents:
        text = replace_mapping_entry(
            text, ("agents", "local", "cli_cmd"), agent.cli_cmd
        )
        text = replace_mapping_entry(
            text, ("agents", "local", "environment"), agent.environment
        )
    else:
        text = replace_mapping_entry(
            text,
            ("agents", "local"),
            agent.model_dump(mode="json", exclude_defaults=True),
        )
    if "invoker" not in config.settings.integrations.model_fields_set:
        text = replace_mapping_entry(
            text,
            ("settings", "integrations", "invoker", "implementation"),
            config.settings.integrations.invoker.implementation,
        )
    if "ollama" not in registrations:
        text = replace_mapping_entry(
            text,
            ("settings", "integrations", "invoker", "options", "providers", "ollama"),
            recipe["ollama"],
        )
    registrations["ollama"] = recipe["ollama"]
    config.agents["local"] = agent
    if load_config_mapping(text, "updated config") != config:
        raise OnboardingRenderingError(
            "Cannot update the Ollama connection without changing other settings."
        )
    return text


def replace_mapping_entry(text: str, keys: tuple[str, ...], value: object) -> str:
    node = yaml.compose(text)
    for offset, name in enumerate(keys):
        if not isinstance(node, MappingNode) or node.flow_style:
            raise OnboardingRenderingError(
                "Ollama setup needs block-style config mappings; no settings were changed."
            )
        match = next(
            (
                (key, child)
                for key, child in node.value
                if isinstance(key, ScalarNode) and key.value == name
            ),
            None,
        )
        if match is None:
            for child_name in reversed(keys[offset + 1 :]):
                value = {child_name: value}
            return rewrite_entry_lines(text, node, None, name, value)
        key, child = match
        if child.start_mark.index < key.end_mark.index:
            raise OnboardingRenderingError(
                "Cannot update an aliased Ollama configuration in place."
            )
        if offset == len(keys) - 1:
            return rewrite_entry_lines(text, node, (key, child), name, value)
        if isinstance(child, MappingNode) and child.flow_style:
            if child.value:
                raise OnboardingRenderingError(
                    "Ollama setup needs block-style config mappings; no settings were changed."
                )
            for child_name in reversed(keys[offset + 1 :]):
                value = {child_name: value}
            return rewrite_entry_lines(text, node, (key, child), name, value)
        node = child
    raise AssertionError("A mapping path must contain at least one key.")


def rewrite_entry_lines(
    text: str,
    parent: MappingNode,
    entry: tuple[yaml.Node, yaml.Node] | None,
    key: str,
    value: object,
) -> str:
    lines = text.splitlines(keepends=True)
    column = parent.value[0][0].start_mark.column
    if entry is None:
        start = end = parent.end_mark.line + (parent.end_mark.column > column)
    else:
        name, node = entry
        start = name.start_mark.line
        end = node.end_mark.line + (node.end_mark.column > column)
        if lines[start][:column].strip():
            raise OnboardingRenderingError(
                "Cannot locate the Ollama setting on its own line."
            )
        # Block end marks include comments belonging to following settings.
        while end > start + 1:
            trailing = lines[end - 1]
            if trailing.strip() and not (
                trailing.lstrip().startswith("#") and leading_spaces(trailing) <= column
            ):
                break
            end -= 1
    replacement = indent(yaml.safe_dump({key: value}, sort_keys=False), " " * column)
    if start and not lines[start - 1].endswith(("\n", "\r")):
        replacement = "\n" + replacement
    lines[start:end] = [replacement]
    result = "".join(lines)
    return result if text.endswith("\n") else result.rstrip("\n")
