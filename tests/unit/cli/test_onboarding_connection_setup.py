import io
import os
from unittest.mock import Mock

import pytest
from rich.console import Console
from yaml import YAMLError

from crewplane.cli.onboarding import (
    OnboardingOptions,
    local_connection,
    write_text_file,
)
from crewplane.cli.onboarding.constants import CONFIG_RELATIVE_PATH
from crewplane.cli.onboarding.plugin_setup import LocalSetupFailure
from crewplane.cli.onboarding.rendering_yaml_loading import load_config_mapping
from crewplane.runtime.agent.process.drain import (
    ProcessDrainError,
    ProcessDrainEvidence,
)


@pytest.fixture
def connection_setup(tmp_path, monkeypatch):
    path = tmp_path / CONFIG_RELATIVE_PATH
    path.parent.mkdir()
    path.write_text(
        'version: "1.0"\n'
        "agents:\n"
        "  local:\n"
        "    cli_cmd: [configured-ollama]\n"
        "    provider_kind: ollama\n"
        "    default_model: kept-model\n",
        encoding="utf-8",
    )
    stream = io.StringIO()
    commands = {
        "configured-ollama": "/configured/ollama",
        "ollama-agent": "/bin/ollama",
        "ollama-agent.exe": "/bin/ollama",
    }
    options = OnboardingOptions(
        project_root=tmp_path,
        console=Console(file=stream, width=180, color_system=None),
        input_is_terminal=True,
        output_is_terminal=True,
        read_input=stream.readline,
        which_fn=commands.get,
        write_text=write_text_file,
        discovery_timeout_seconds=2.5,
    )
    monkeypatch.setattr(
        local_connection.metadata, "version", Mock(return_value="0.1.0")
    )
    return options, path, stream


@pytest.mark.parametrize(
    "outcome", ["saved", "save-declined", "cancelled", "unavailable"]
)
def test_connection_setup_preserves_discovery_context_and_handled_results(
    connection_setup, monkeypatch, outcome
):
    options, path, stream = connection_setup
    original = path.read_bytes()
    existing = load_config_mapping(original.decode("utf-8"), "before").agents["local"]
    prompts = []

    def prompt(text):
        prompts.append(text)
        return "1"

    def confirm(text, default):
        prompts.append((text, default))
        return default is False or outcome != "save-declined"

    def prepare(request, console, select, agent):
        assert request.project_root == options.project_root
        assert request.environment == os.environ
        assert request.environment is not os.environ
        assert request.timeout_seconds == 2.5
        command = "ollama-agent.exe" if os.name == "nt" else "ollama-agent"
        assert request.executable_lookup(command) == "/configured/ollama"
        assert console is options.console
        assert agent == existing
        assert select("Choose endpoint: ") == "1"
        if outcome == "cancelled":
            return LocalSetupFailure.CANCELLED
        if outcome == "unavailable":
            return LocalSetupFailure.UNAVAILABLE
        return agent.model_copy(update={"cli_cmd": ["/configured/ollama"]})

    monkeypatch.setattr(local_connection, "prepare_local_agent", prepare)

    assert local_connection.configure_local_connection(options, prompt, confirm) is True

    expected = [
        ("Configure the Ollama connection? [y/N]: ", False),
        "Choose endpoint: ",
    ]
    if outcome in {"saved", "save-declined"}:
        expected.append(("Save the Ollama connection in config.yml? [Y/n]: ", True))
    assert prompts == expected
    assert not list(path.parent.glob(".ollama-*"))
    if outcome == "saved":
        agent = load_config_mapping(path.read_text(), "after").agents["local"]
        assert agent == existing.model_copy(update={"cli_cmd": ["/configured/ollama"]})
        assert "Ollama connection saved" in stream.getvalue()
    else:
        assert path.read_bytes() == original
        assert "No files changed." in stream.getvalue()
        assert "Ollama connection saved" not in stream.getvalue()
        if outcome == "cancelled":
            assert "Ollama setup was cancelled." in stream.getvalue()
        elif outcome == "unavailable":
            assert "Ollama setup could not finish." in stream.getvalue()


@pytest.mark.parametrize(
    ("error_type", "unconfirmed_cleanup", "handled"),
    [
        (OSError, False, True),
        (ValueError, False, True),
        (YAMLError, False, True),
        (ValueError, True, False),
        (TimeoutError, True, False),
        (RuntimeError, False, False),
        (EOFError, False, False),
        (KeyboardInterrupt, False, False),
    ],
)
def test_connection_setup_preserves_exception_boundary(
    connection_setup, monkeypatch, error_type, unconfirmed_cleanup, handled
):
    options, path, stream = connection_setup
    original = path.read_bytes()
    error = error_type("discovery failure")
    if unconfirmed_cleanup:
        error.__cause__ = ProcessDrainError(
            ProcessDrainEvidence(42101, 42102, True, False), "cleanup unconfirmed"
        )
    monkeypatch.setattr(
        local_connection, "prepare_local_agent", Mock(side_effect=error)
    )
    prompt = Mock()
    confirm = Mock(return_value=True)

    if handled:
        assert (
            local_connection.configure_local_connection(options, prompt, confirm)
            is True
        )
        output = stream.getvalue()
        assert output.index("discovery failure") < output.index(
            "Ollama was not configured."
        )
        assert "No files changed." in output
    else:
        with pytest.raises(error_type) as caught:
            local_connection.configure_local_connection(options, prompt, confirm)
        assert caught.value is error
        assert stream.getvalue() == ""
    assert path.read_bytes() == original
    assert not list(path.parent.glob(".ollama-*"))
    confirm.assert_called_once_with("Configure the Ollama connection? [y/N]: ", False)
    prompt.assert_not_called()
