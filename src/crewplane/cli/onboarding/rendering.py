from __future__ import annotations

from collections.abc import Mapping

from yaml import YAMLError

from crewplane.core.config import Config
from crewplane.core.provider_names import known_provider_names
from crewplane.core.yaml_loader import load_yaml_unique

from ..templates import (
    CONFIG_TEMPLATE,
    DEFAULT_WORKFLOW_TEMPLATE,
    render_template_content,
)

KNOWN_PROVIDER_NAMES = known_provider_names()


class OnboardingRenderingError(ValueError):
    """Raised when current packaged defaults cannot be transformed safely."""


def rendered_default_config() -> str:
    return render_template_content(CONFIG_TEMPLATE.read_text(encoding="utf-8"))


def rendered_default_workflow() -> str:
    return render_template_content(
        DEFAULT_WORKFLOW_TEMPLATE.read_text(encoding="utf-8")
    )


def render_provider_ready_config(default_config: str, provider: str) -> str:
    validate_known_provider(provider)
    text = comment_mock_agent(default_config)
    text = uncomment_provider_agent(text, provider)
    text = activate_cli_invoker(text)
    validate_provider_ready_config(text, provider)
    return text


def render_provider_ready_workflow(default_workflow: str, provider: str) -> str:
    validate_known_provider(provider)
    lines, provider_line_index = workflow_provider_line(default_workflow, "mock")
    indent = line_indent(lines[provider_line_index])
    lines[provider_line_index] = f"{indent}providers: [{provider}]"
    text = join_lines_like(default_workflow, lines)
    validate_provider_ready_workflow(text, provider)
    return text


def manual_config_snippet(default_config: str, provider: str) -> str:
    validate_known_provider(provider)
    agent_block = uncomment_comment_block(
        extract_commented_provider_block(default_config, provider)
    ).strip()
    return "\n\n".join((agent_block, manual_cli_invoker_snippet(default_config)))


def manual_workflow_snippet(default_workflow: str, provider: str) -> str:
    validate_known_provider(provider)
    provider_ready_workflow = render_provider_ready_workflow(
        default_workflow,
        provider,
    )
    lines, frontmatter_start, frontmatter_end = workflow_frontmatter_bounds(
        provider_ready_workflow
    )
    nodes_index = frontmatter_mapping_key_line_index(
        lines,
        frontmatter_start,
        frontmatter_end,
        "nodes",
        0,
        "default workflow nodes",
    )
    nodes_end = yaml_child_block_end(
        lines, nodes_index, leading_spaces(lines[nodes_index])
    )
    return "\n".join(lines[nodes_index:nodes_end])


def validate_known_provider(provider: str) -> None:
    if provider not in KNOWN_PROVIDER_NAMES:
        names = ", ".join(KNOWN_PROVIDER_NAMES)
        raise OnboardingRenderingError(
            f"Unknown provider '{provider}'. Expected {names}."
        )


def comment_mock_agent(default_config: str) -> str:
    start_marker = "  mock:\n"
    end_marker = "\n\n  # Real provider examples"
    start = default_config.find(start_marker)
    if start < 0:
        raise OnboardingRenderingError("Default config is missing the mock agent.")
    end = default_config.find(end_marker, start)
    if end < 0:
        raise OnboardingRenderingError(
            "Default config is missing the provider examples."
        )
    mock_block = default_config[start:end]
    return (
        default_config[:start]
        + comment_yaml_block(mock_block, 2)
        + default_config[end:]
    )


def uncomment_provider_agent(default_config: str, provider: str) -> str:
    block = extract_commented_provider_block(default_config, provider)
    return replace_once(
        default_config,
        block,
        uncomment_comment_block(block),
        f"{provider} provider example",
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


def extract_commented_provider_block(default_config: str, provider: str) -> str:
    marker = f"  # {provider}:\n"
    start = default_config.find(marker)
    if start < 0:
        raise OnboardingRenderingError(f"Default config is missing {provider}.")
    end = provider_block_end(default_config, provider, start)
    return default_config[start:end]


def provider_block_end(default_config: str, provider: str, start: int) -> int:
    candidates = [
        default_config.find(f"\n  # {name}:", start + 1)
        for name in KNOWN_PROVIDER_NAMES
        if name != provider
    ]
    settings_start = default_config.find("\nsettings:", start)
    candidates.append(settings_start)
    positive_candidates = [index for index in candidates if index > start]
    if not positive_candidates:
        raise OnboardingRenderingError(f"Default config cannot bound {provider}.")
    return min(positive_candidates)


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


def comment_yaml_block(block: str, indent: int) -> str:
    prefix = " " * indent
    lines = []
    for line in block.splitlines():
        if line == "" or line.startswith(f"{prefix}#"):
            lines.append(line)
        elif line.startswith(prefix):
            lines.append(f"{prefix}# {line[indent:]}")
        else:
            raise OnboardingRenderingError("Default config block indentation changed.")
    return "\n".join(lines)


def uncomment_comment_block(block: str) -> str:
    return uncomment_yaml_comment_block(block, 2)


def uncomment_yaml_comment_block(block: str, indent: int) -> str:
    prefix = " " * indent
    comment_prefix = f"{prefix}# "
    blank_comment = f"{prefix}#"
    lines = []
    for line in block.splitlines():
        if line == blank_comment:
            lines.append("")
        elif line.startswith(comment_prefix):
            lines.append(f"{prefix}{line[len(comment_prefix) :]}")
        else:
            raise OnboardingRenderingError("Default comment block changed.")
    return "\n".join(lines)


def replace_once(text: str, old: str, new: str, description: str) -> str:
    count = text.count(old)
    if count != 1:
        raise OnboardingRenderingError(
            f"Expected one {description} anchor, found {count}."
        )
    return text.replace(old, new, 1)


def validate_provider_ready_config(config_text: str, provider: str) -> None:
    config = load_config_mapping(config_text, "provider-ready config")
    if list(config.agents) != [provider]:
        raise OnboardingRenderingError(
            "Provider-ready config must contain only the selected provider."
        )
    provider_kind = config.agents[provider].provider_kind
    if provider_kind.value != provider:
        raise OnboardingRenderingError(
            f"Provider-ready config for {provider} has provider_kind "
            f"{provider_kind.value!r}."
        )
    if config.settings is None:
        raise OnboardingRenderingError("Provider-ready config is missing settings.")
    invoker = config.settings.integrations.invoker
    if invoker.implementation != "cli":
        raise OnboardingRenderingError(
            "Provider-ready config must activate the cli invoker."
        )
    if invoker.options != {}:
        raise OnboardingRenderingError(
            "Provider-ready config must not leave mock options under cli."
        )


def load_config_mapping(config_text: str, description: str) -> Config:
    data = load_yaml_mapping(config_text, description)
    try:
        return Config.model_validate(data)
    except ValueError as error:
        raise OnboardingRenderingError(f"{description} is invalid.") from error


def load_yaml_mapping(text: str, description: str) -> Mapping[str, object]:
    try:
        data = load_yaml_unique(text)
    except (TypeError, ValueError, YAMLError) as error:
        raise OnboardingRenderingError(f"{description} is not valid YAML.") from error
    if not isinstance(data, Mapping):
        raise OnboardingRenderingError(f"{description} must be a YAML mapping.")
    return data


def mapping_child(
    data: Mapping[str, object],
    key: str,
    description: str,
) -> Mapping[str, object]:
    value = data.get(key)
    if not isinstance(value, Mapping):
        raise OnboardingRenderingError(f"{description} is missing {key}.")
    return value


def validate_provider_ready_workflow(workflow_text: str, provider: str) -> None:
    providers = workflow_frontmatter_providers(workflow_text)
    if providers != [provider]:
        raise OnboardingRenderingError(
            "Provider-ready workflow must reference only the selected provider."
        )


def workflow_provider_line(
    workflow_text: str,
    expected_provider: str,
) -> tuple[list[str], int]:
    lines, frontmatter_start, frontmatter_end = workflow_frontmatter_bounds(
        workflow_text
    )
    providers = workflow_frontmatter_providers_from_lines(
        lines,
        frontmatter_start,
        frontmatter_end,
    )
    if providers != [expected_provider]:
        raise OnboardingRenderingError(
            "Default workflow provider list is not the generated mock setup."
        )
    provider_line_indices = [
        index
        for index in range(frontmatter_start, frontmatter_end)
        if lines[index].lstrip().startswith("providers:")
    ]
    if len(provider_line_indices) != 1:
        raise OnboardingRenderingError(
            "Default workflow must contain one providers line."
        )
    return lines, provider_line_indices[0]


def workflow_frontmatter_providers(workflow_text: str) -> list[str]:
    lines, frontmatter_start, frontmatter_end = workflow_frontmatter_bounds(
        workflow_text
    )
    return workflow_frontmatter_providers_from_lines(
        lines,
        frontmatter_start,
        frontmatter_end,
    )


def workflow_frontmatter_providers_from_lines(
    lines: list[str],
    frontmatter_start: int,
    frontmatter_end: int,
) -> list[str]:
    data = load_yaml_mapping(
        "\n".join(lines[frontmatter_start:frontmatter_end]),
        "default workflow frontmatter",
    )
    nodes = data.get("nodes")
    if not isinstance(nodes, list) or len(nodes) != 1:
        raise OnboardingRenderingError(
            "Default workflow must contain one frontmatter node."
        )
    node = nodes[0]
    if not isinstance(node, Mapping):
        raise OnboardingRenderingError("Default workflow node must be a mapping.")
    providers = node.get("providers")
    if not isinstance(providers, list) or not all(
        isinstance(provider, str) for provider in providers
    ):
        raise OnboardingRenderingError(
            "Default workflow node must contain provider names."
        )
    return providers


def workflow_frontmatter_bounds(workflow_text: str) -> tuple[list[str], int, int]:
    lines = workflow_text.splitlines()
    if not lines or lines[0] != "---":
        raise OnboardingRenderingError("Default workflow is missing frontmatter.")
    try:
        frontmatter_end = lines.index("---", 1)
    except ValueError as error:
        raise OnboardingRenderingError(
            "Default workflow frontmatter is not closed."
        ) from error
    return lines, 1, frontmatter_end


def mapping_key_line_index(
    lines: list[str],
    key: str,
    indent: int,
    description: str,
) -> int:
    marker = f"{' ' * indent}{key}:"
    indices = [index for index, line in enumerate(lines) if line == marker]
    if len(indices) != 1:
        raise OnboardingRenderingError(
            f"Expected one {description} block, found {len(indices)}."
        )
    return indices[0]


def frontmatter_mapping_key_line_index(
    lines: list[str],
    frontmatter_start: int,
    frontmatter_end: int,
    key: str,
    indent: int,
    description: str,
) -> int:
    marker = f"{' ' * indent}{key}:"
    indices = [
        index
        for index in range(frontmatter_start, frontmatter_end)
        if lines[index] == marker
    ]
    if len(indices) != 1:
        raise OnboardingRenderingError(
            f"Expected one {description} block, found {len(indices)}."
        )
    return indices[0]


def active_mapping_key_line_index(
    lines: list[str],
    parent_index: int,
    key: str,
    description: str,
) -> int:
    parent_indent = leading_spaces(lines[parent_index])
    child_indent = parent_indent + 2
    for index in range(parent_index + 1, len(lines)):
        line = lines[index]
        if line.strip() and leading_spaces(line) <= parent_indent:
            break
        if leading_spaces(line) == child_indent and line.strip().startswith(f"{key}:"):
            return index
    raise OnboardingRenderingError(f"Default config is missing {description}.")


def yaml_child_block_end(
    lines: list[str],
    start_index: int,
    parent_indent: int,
) -> int:
    for index in range(start_index + 1, len(lines)):
        line = lines[index]
        if line.strip() and leading_spaces(line) <= parent_indent:
            return index
    return len(lines)


def leading_spaces(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def line_indent(line: str) -> str:
    return line[: leading_spaces(line)]


def join_lines_like(original: str, lines: list[str]) -> str:
    text = "\n".join(lines)
    if original.endswith("\n"):
        return f"{text}\n"
    return text


__all__ = [
    "KNOWN_PROVIDER_NAMES",
    "OnboardingRenderingError",
    "manual_config_snippet",
    "manual_workflow_snippet",
    "render_provider_ready_config",
    "render_provider_ready_workflow",
    "rendered_default_config",
    "rendered_default_workflow",
]
