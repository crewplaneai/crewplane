from __future__ import annotations

from .rendering_errors import OnboardingRenderingError
from .rendering_providers import KNOWN_PROVIDER_NAMES
from .rendering_yaml_blocks import (
    comment_yaml_block,
    replace_once,
    uncomment_comment_block,
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


__all__ = [
    "comment_mock_agent",
    "extract_commented_provider_block",
    "uncomment_provider_agent",
]
