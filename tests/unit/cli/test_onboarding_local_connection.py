from textwrap import dedent

import pytest

from crewplane.cli.onboarding import write_text_file
from crewplane.cli.onboarding.local_connection import save_local_connection
from crewplane.cli.onboarding.rendering_errors import OnboardingRenderingError
from crewplane.cli.onboarding.rendering_local_connection import render_local_connection
from crewplane.cli.onboarding.rendering_yaml_loading import load_config_mapping
from crewplane.core.config import AgentConfig
from tests.helpers.platforms import requires_posix


@pytest.fixture
def local_agent():
    return AgentConfig(
        cli_cmd=["/usr/bin/ollama", "run"],
        provider_kind="ollama",
        default_model="kept-model",
        environment={"OLLAMA_HOST": "http://172.20.0.1:11434"},
    )


@pytest.mark.parametrize(
    "settings",
    [
        "",
        "settings: {}\n",
        "settings:\n  integrations: {}\n",
        "settings:\n  integrations:\n    ui:\n      implementation: tmux\n",
        "settings:\n  integrations:\n    invoker:\n      implementation: cli\n",
        "settings:\n  integrations:\n    invoker:\n      implementation: cli\n"
        "      options: {}\n",
        "settings:\n  integrations:\n    invoker:\n      implementation: cli\n"
        "      options:\n        providers: {}\n",
    ],
    ids=[
        "implicit-defaults",
        "empty-settings",
        "empty-integrations",
        "other-integration",
        "explicit-cli",
        "empty-options",
        "empty-providers",
    ],
)
@pytest.mark.parametrize("ending", ["", "\n", "\n...\n"])
def test_add_connection_preserves_existing_config(local_agent, settings, ending):
    original = dedent("""\
        version: "1.0"
        # This provider and its formatting belong to the user.
        agents:
          codex:
            cli_cmd: [codex, exec]
            provider_kind: codex
        """)
    original = (original + settings).rstrip("\n") + ending
    before = load_config_mapping(original, "before")

    rendered = render_local_connection(original, local_agent)

    after = load_config_mapping(rendered, "after")
    assert after.agents["codex"] == before.agents["codex"]
    assert after.agents["local"] == local_agent
    assert after.settings.integrations.invoker.implementation == "cli"
    assert after.settings.integrations.ui == before.settings.integrations.ui
    assert "# This provider and its formatting belong to the user." in rendered
    assert "    cli_cmd: [codex, exec]" in rendered
    assert rendered.endswith("\n") == original.endswith("\n")
    assert after.settings.integrations.invoker.options == {
        "providers": {
            "ollama": {
                "implementation": "crewplane_ollama:OLLAMA",
                "distribution": "crewplane-ollama",
            }
        }
    }


def test_connection_update_inserts_environment_without_changing_model(local_agent):
    original = dedent("""\
        version: "1.0"
        settings:
          integrations:
            invoker:
              implementation: cli
        agents:
          local:
            cli_cmd:
            - old-ollama
            - run
            provider_kind: ollama
            default_model: "kept-model"  # Keep this choice.
        """)

    rendered = render_local_connection(original, local_agent)

    assert load_config_mapping(rendered, "after").agents["local"] == local_agent
    assert 'default_model: "kept-model"  # Keep this choice.' in rendered


def test_connection_update_preserves_following_comments_and_multiline_values(
    local_agent,
):
    original = dedent("""\
        version: "1.0"
        agents:
          local:
            cli_cmd:
            - old-ollama
            - run

            # Keep invocation notes.
            provider_kind: ollama
            default_model: kept-model
            environment:
              OLLAMA_HOST: http://old-host:11434
              NOTES: |+
                # This is part of the value.

          # backup:
          #   cli_cmd: [backup-ollama, run]
        # Keep project settings.
        settings:
          integrations:
            invoker:
              implementation: cli
        """)
    before = load_config_mapping(original, "before")
    agent = local_agent.model_copy(
        update={
            "environment": {
                **before.agents["local"].environment,
                **local_agent.environment,
            }
        }
    )

    rendered = render_local_connection(original, agent)

    assert load_config_mapping(rendered, "after").agents["local"] == agent
    assert agent.environment["NOTES"] == "# This is part of the value.\n\n"
    assert "\n\n    # Keep invocation notes.\n    provider_kind: ollama" in rendered
    assert (
        "\n  # backup:\n  #   cli_cmd: [backup-ollama, run]\n"
        "# Keep project settings.\nsettings:\n"
    ) in rendered


def test_add_connection_preserves_anchored_agents_mapping(local_agent):
    original = dedent("""\
        version: "1.0"
        agents: &profiles
          codex:
            cli_cmd: [codex, exec]
            provider_kind: codex
        """)
    before = load_config_mapping(original, "before")

    rendered = render_local_connection(original, local_agent)

    after = load_config_mapping(rendered, "after")
    assert after.agents["local"] == local_agent
    assert after.agents["codex"] == before.agents["codex"]
    assert "agents: &profiles\n" in rendered


@pytest.mark.parametrize(
    "agents",
    [
        "agents: {local: {cli_cmd: [ollama, run], provider_kind: ollama}}",
        "agents:\n  other: &base\n    cli_cmd: [ollama, run]\n    provider_kind: ollama\n  local: *base",
    ],
)
def test_connection_rejects_unsafe_in_place_edits(local_agent, agents):
    with pytest.raises(OnboardingRenderingError, match="block-style|aliased"):
        render_local_connection('version: "1.0"\n' + agents + "\n", local_agent)


def test_connection_preserves_custom_plugin_registration(local_agent):
    original = dedent("""\
        version: "1.0"
        agents: {}
        settings:
          integrations:
            invoker:
              implementation: cli
              options:
                providers:
                  ollama:
                    implementation: custom_plugin:CAPABILITY
                    distribution: custom-plugin
        """)
    with pytest.raises(OnboardingRenderingError, match="customized"):
        render_local_connection(original, local_agent)


def test_connection_renderer_rejects_unrelated_agent_changes(local_agent):
    original = dedent("""\
        version: "1.0"
        settings:
          integrations:
            invoker:
              implementation: cli
        agents:
          local:
            cli_cmd: [ollama, run]
            provider_kind: ollama
            default_model: different-model
        """)
    with pytest.raises(OnboardingRenderingError, match="other settings"):
        render_local_connection(original, local_agent)


@requires_posix
def test_connection_save_preserves_config_symlink_and_permissions(tmp_path):
    target = tmp_path / "project-config.yml"
    target.write_bytes(b"original\n")
    target.chmod(0o640)
    path = tmp_path / "config.yml"
    path.symlink_to(target.name)

    save_local_connection(path, b"original\n", "updated\n", write_text_file)

    assert path.is_symlink()
    assert target.read_bytes() == b"updated\n"
    assert target.stat().st_mode & 0o777 == 0o640
    assert set(tmp_path.iterdir()) == {path, target}
