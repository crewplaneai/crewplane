from textwrap import dedent

import pytest
from yaml import YAMLError

from crewplane.cli.onboarding.rendering_errors import OnboardingRenderingError
from crewplane.cli.onboarding.rendering_local_connection import (
    render_local_connection,
    replace_mapping_entry,
)
from crewplane.cli.onboarding.rendering_yaml_loading import load_config_mapping
from crewplane.core.config import AgentConfig


@pytest.mark.parametrize("existing", ["", "\n  local: {}"])
@pytest.mark.parametrize("ending", ["", "\n"])
def test_mapping_insertion_preserves_path_and_value_order(existing, ending):
    original = "root:\n  keep: 1" + existing + ending

    rendered = replace_mapping_entry(
        original, ("root", "local", "nested"), {"first": 2, "second": 3}
    )

    assert rendered == (
        "root:\n  keep: 1\n  local:\n    nested:\n      first: 2\n      second: 3"
        + ending
    )


@pytest.mark.parametrize(
    ("original", "keys", "error_type", "message"),
    [
        (
            "root: value\n",
            (),
            AssertionError,
            "A mapping path must contain at least one key.",
        ),
        (
            "value\n",
            ("root",),
            OnboardingRenderingError,
            "Ollama setup needs block-style config mappings; no settings were changed.",
        ),
        (
            "{root: value}\n",
            ("root",),
            OnboardingRenderingError,
            "Ollama setup needs block-style config mappings; no settings were changed.",
        ),
        (
            "root: value\n",
            ("root", "local"),
            OnboardingRenderingError,
            "Ollama setup needs block-style config mappings; no settings were changed.",
        ),
        (
            "root: {keep: 1}\n",
            ("root", "local"),
            OnboardingRenderingError,
            "Ollama setup needs block-style config mappings; no settings were changed.",
        ),
        (
            "base: &base {}\nroot: *base\n",
            ("root", "local"),
            OnboardingRenderingError,
            "Cannot update an aliased Ollama configuration in place.",
        ),
        (
            "? root\n: value\n",
            ("root",),
            OnboardingRenderingError,
            "Cannot locate the Ollama setting on its own line.",
        ),
    ],
)
def test_mapping_replacement_preserves_errors(original, keys, error_type, message):
    with pytest.raises(error_type) as caught:
        replace_mapping_entry(original, keys, "replacement")

    assert str(caught.value) == message


def test_mapping_replacement_propagates_yaml_errors():
    with pytest.raises(YAMLError):
        replace_mapping_entry("root: [\n", ("root",), "replacement")


@pytest.mark.parametrize("registrations", ["[]", "null", "custom"])
def test_connection_rejects_non_mapping_registrations_before_rendering(registrations):
    original = (
        dedent("""\
        version: "1.0"
        agents: {local: {cli_cmd: [old-ollama, run], provider_kind: ollama}}
        settings:
          integrations:
            invoker:
              implementation: cli
              options:
        """)
        + f"        providers: {registrations}\n"
    )
    agent = AgentConfig(cli_cmd=["ollama", "run"], provider_kind="ollama")

    with pytest.raises(OnboardingRenderingError) as caught:
        render_local_connection(original, agent)

    assert str(caught.value) == "CLI provider registrations must be a mapping."


def test_connection_preserves_existing_registration_text_and_agent_input():
    settings = dedent("""\
        settings:
          integrations:
            invoker:
              implementation: cli
              options:
                providers:
                  ollama:  # Keep this registration.
                    distribution: "crewplane-ollama"
                    implementation: "crewplane_ollama:OLLAMA"
                  custom:
                    implementation: custom_provider:CAPABILITY
                    distribution: custom-provider
        """)
    original = 'version: "1.0"\nagents: {}\n' + settings
    agent = AgentConfig(cli_cmd=["ollama", "run"], provider_kind="ollama")
    before_agent = agent.model_copy(deep=True)
    before = load_config_mapping(original, "before")

    rendered = render_local_connection(original, agent)

    after = load_config_mapping(rendered, "after")
    assert rendered.endswith(settings)
    assert after.settings == before.settings
    assert after.agents == {"local": before_agent}
    assert agent == before_agent
