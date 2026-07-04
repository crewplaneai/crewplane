from __future__ import annotations

from collections.abc import Mapping

from yaml import YAMLError

from crewplane.core.config import Config
from crewplane.core.yaml_loader import load_yaml_unique

from .rendering_errors import OnboardingRenderingError


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


__all__ = [
    "load_config_mapping",
    "load_yaml_mapping",
    "mapping_child",
]
