from __future__ import annotations

from .rendering_errors import OnboardingRenderingError
from .rendering_yaml_blocks import (
    active_mapping_key_line_index,
    comment_yaml_block,
    leading_spaces,
    mapping_key_line_index,
    replace_once,
    uncomment_yaml_comment_block,
    yaml_child_block_end,
)
from .rendering_yaml_loading import (
    load_config_mapping,
    load_yaml_mapping,
    mapping_child,
)


def activate_cli_invoker(default_config: str) -> str:
    cli_invoker_block = extract_commented_cli_invoker_block(default_config)
    active_mock_invoker_block = extract_active_mock_invoker_block(default_config)
    text = replace_once(
        default_config,
        cli_invoker_block,
        uncomment_yaml_comment_block(cli_invoker_block, 6),
        "commented cli invoker block",
    )
    return replace_once(
        text,
        active_mock_invoker_block,
        comment_yaml_block(active_mock_invoker_block, 6),
        "active mock invoker block",
    )


def manual_cli_invoker_snippet(default_config: str) -> str:
    lines = default_config.splitlines()
    settings_index = mapping_key_line_index(lines, "settings", 0, "settings")
    integrations_index = mapping_key_line_index(
        lines,
        "integrations",
        2,
        "settings.integrations",
    )
    invoker_index = mapping_key_line_index(
        lines,
        "invoker",
        4,
        "settings.integrations.invoker",
    )
    cli_invoker_block = extract_commented_cli_invoker_block(default_config)
    return "\n".join(
        (
            lines[settings_index],
            lines[integrations_index],
            lines[invoker_index],
            uncomment_yaml_comment_block(cli_invoker_block, 6),
        )
    )


def extract_commented_cli_invoker_block(default_config: str) -> str:
    lines = default_config.splitlines()
    invoker_index = mapping_key_line_index(
        lines,
        "invoker",
        4,
        "settings.integrations.invoker",
    )
    parent_indent = leading_spaces(lines[invoker_index])
    child_indent = parent_indent + 2
    for index in range(invoker_index + 1, len(lines) - 1):
        line = lines[index]
        if line.strip() and leading_spaces(line) <= parent_indent:
            break
        candidate = "\n".join(lines[index : index + 2])
        if is_commented_cli_invoker_block(candidate, child_indent):
            return candidate
    raise OnboardingRenderingError(
        "Default config is missing the commented cli invoker block."
    )


def is_commented_cli_invoker_block(block: str, indent: int) -> bool:
    try:
        uncommented_block = uncomment_yaml_comment_block(block, indent)
        data = load_yaml_mapping(
            "settings:\n  integrations:\n    invoker:\n" + uncommented_block,
            "commented cli invoker block",
        )
        settings = mapping_child(data, "settings", "commented cli invoker block")
        integrations = mapping_child(
            settings,
            "integrations",
            "commented cli invoker block",
        )
        invoker = mapping_child(
            integrations,
            "invoker",
            "commented cli invoker block",
        )
    except OnboardingRenderingError:
        return False
    return invoker.get("implementation") == "cli" and invoker.get("options") == {}


def extract_active_mock_invoker_block(default_config: str) -> str:
    config = load_config_mapping(default_config, "default config")
    if config.settings is None:
        raise OnboardingRenderingError("Default config is missing settings.")
    invoker = config.settings.integrations.invoker
    if invoker.implementation != "mock":
        raise OnboardingRenderingError("Default config is not using the mock invoker.")

    lines = default_config.splitlines()
    invoker_index = mapping_key_line_index(
        lines,
        "invoker",
        4,
        "settings.integrations.invoker",
    )
    implementation_index = active_mapping_key_line_index(
        lines,
        invoker_index,
        "implementation",
        "active mock invoker implementation",
    )
    block_end = yaml_child_block_end(
        lines,
        implementation_index,
        leading_spaces(lines[invoker_index]),
    )
    return "\n".join(lines[implementation_index:block_end])


__all__ = [
    "activate_cli_invoker",
    "manual_cli_invoker_snippet",
]
