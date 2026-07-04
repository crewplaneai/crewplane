from __future__ import annotations

from .rendering_errors import OnboardingRenderingError
from .rendering_yaml_loading import load_config_mapping


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


__all__ = ["validate_provider_ready_config"]
