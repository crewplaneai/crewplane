from __future__ import annotations

from textwrap import indent

import yaml
from yaml.nodes import MappingNode, ScalarNode

from crewplane.architecture.contracts import JsonObject
from crewplane.core.config import AgentConfig

from .plugin_profile import ollama_provider_options
from .rendering_errors import OnboardingRenderingError
from .rendering_yaml_blocks import leading_spaces
from .rendering_yaml_loading import load_config_mapping


def render_local_connection(text: str, agent: AgentConfig) -> str:
    """Render a local connection without writing files or mutating the agent.

    An existing local agent may differ only in cli_cmd and environment; a new
    agent is rendered with defaults omitted. Add the default Ollama registration
    if missing, preserve an identical registration, and reject a customized one.
    Materialize the default invoker implementation when it was implicit.

    Edited paths must use block mappings, with empty flow mappings permitted
    for missing sections. Preserve unrelated config values and source text,
    including following comments and the presence of a final newline.

    Raises:
        OnboardingRenderingError: Config is invalid, registrations are unsafe,
            an edit is unsupported, or the result changes unrelated settings.
        yaml.YAMLError: An intermediate edit leaves YAML that cannot be composed,
            such as removing an anchor that is still referenced.
    """
    config = load_config_mapping(text, "existing config")
    registrations, recipe = _prepare_plugin_registration(
        config.settings.integrations.invoker.options
    )
    text = _render_connection_agent(text, agent, config.agents.get("local"))
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


def _prepare_plugin_registration(options: JsonObject) -> tuple[JsonObject, JsonObject]:
    registrations = options.setdefault("providers", {})
    recipe = ollama_provider_options()["providers"]
    assert isinstance(recipe, dict)
    if not isinstance(registrations, dict):
        raise OnboardingRenderingError("CLI provider registrations must be a mapping.")
    if "ollama" in registrations and registrations["ollama"] != recipe["ollama"]:
        raise OnboardingRenderingError(
            "The existing Ollama plugin registration is customized; leave it unchanged."
        )
    return registrations, recipe


def _render_connection_agent(
    text: str, agent: AgentConfig, existing: AgentConfig | None
) -> str:
    if existing is None:
        return replace_mapping_entry(
            text,
            ("agents", "local"),
            agent.model_dump(mode="json", exclude_defaults=True),
        )
    text = replace_mapping_entry(text, ("agents", "local", "cli_cmd"), agent.cli_cmd)
    return replace_mapping_entry(
        text, ("agents", "local", "environment"), agent.environment
    )


def replace_mapping_entry(text: str, keys: tuple[str, ...], value: object) -> str:
    """Replace a YAML entry or append its missing path in the supplied order.

    Traverse block mappings along nonempty keys, expanding empty flow mappings
    when descending. Replace the leaf with safe-dumped value, preserving text
    outside the entry and whether text ends in a newline. Replaced entries lose
    their own formatting and comments; following peer comments are retained.

    Raises:
        AssertionError: keys is empty after YAML composition succeeds.
        OnboardingRenderingError: A traversed mapping is unsupported, a matched
            value is an alias, or the entry does not occupy its own line.
        yaml.YAMLError: YAML composition or safe serialization fails.
    """
    node = yaml.compose(text)
    for offset, name in enumerate(keys):
        if not isinstance(node, MappingNode) or node.flow_style:
            raise OnboardingRenderingError(
                "Ollama setup needs block-style config mappings; no settings were changed."
            )
        match = _find_mapping_entry(node, name)
        if match is None:
            value = _build_nested_mapping(keys[offset + 1 :], value)
            return rewrite_entry_lines(text, node, None, name, value)
        key, child = match
        if offset == len(keys) - 1:
            return rewrite_entry_lines(text, node, (key, child), name, value)
        if isinstance(child, MappingNode) and child.flow_style:
            if child.value:
                raise OnboardingRenderingError(
                    "Ollama setup needs block-style config mappings; no settings were changed."
                )
            value = _build_nested_mapping(keys[offset + 1 :], value)
            return rewrite_entry_lines(text, node, (key, child), name, value)
        node = child
    raise AssertionError("A mapping path must contain at least one key.")


def _find_mapping_entry(
    parent: MappingNode, name: str
) -> tuple[yaml.Node, yaml.Node] | None:
    match = next(
        (
            (key, child)
            for key, child in parent.value
            if isinstance(key, ScalarNode) and key.value == name
        ),
        None,
    )
    if match is not None:
        key, child = match
        if child.start_mark.index < key.end_mark.index:
            raise OnboardingRenderingError(
                "Cannot update an aliased Ollama configuration in place."
            )
    return match


def _build_nested_mapping(keys: tuple[str, ...], value: object) -> object:
    for name in reversed(keys):
        value = {name: value}
    return value


def rewrite_entry_lines(
    text: str,
    parent: MappingNode,
    entry: tuple[yaml.Node, yaml.Node] | None,
    key: str,
    value: object,
) -> str:
    """Splice a safe-dumped entry into source text using composed YAML marks.

    parent must be a nonempty mapping composed from text; entry must contain
    nodes from that mapping, or be None to append. Callers validate mapping
    style and aliases. Keep following blank lines and comments at or above the
    entry's indentation, and preserve whether text ends in a newline.

    Raises:
        OnboardingRenderingError: The existing key does not start on its own line.
        yaml.YAMLError: key or value cannot be safely serialized.
    """
    lines = text.splitlines(keepends=True)
    column = parent.value[0][0].start_mark.column
    if entry is None:
        start = end = parent.end_mark.line + (parent.end_mark.column > column)
    else:
        start, end = _entry_line_span(lines, entry, column)
    replacement = indent(yaml.safe_dump({key: value}, sort_keys=False), " " * column)
    if start and not lines[start - 1].endswith(("\n", "\r")):
        replacement = "\n" + replacement
    lines[start:end] = [replacement]
    result = "".join(lines)
    return result if text.endswith("\n") else result.rstrip("\n")


def _entry_line_span(
    lines: list[str], entry: tuple[yaml.Node, yaml.Node], column: int
) -> tuple[int, int]:
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
    return start, end
