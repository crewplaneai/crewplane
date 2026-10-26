import asyncio
import io
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from rich.console import Console

from crewplane.adapters.invokers.cli_invoker import (
    CliDiscoveredEndpoint,
    CliDiscoveredModels,
    CliDiscoveryRequest,
    CliProviderDiscovery,
    CliProviderOnboarding,
)
from crewplane.cli.onboarding import plugin_setup
from crewplane.core.config import AgentConfig


@pytest.mark.parametrize("has_existing_agent", [False, True])
def test_local_setup_preserves_selection_order_and_callback_lifetimes(
    tmp_path, monkeypatch, has_existing_agent
):
    stream = io.StringIO()
    console = Console(file=stream, width=180, color_system=None)
    request = CliDiscoveryRequest(tmp_path, {}, {}.get, 37)
    endpoints = (
        CliDiscoveredEndpoint("first server", {"OLLAMA_HOST": "http://first:11434"}),
        CliDiscoveredEndpoint(
            "second [server]", {"OLLAMA_HOST": "http://second:11434"}
        ),
    )
    command = ("/new/ollama-agent",)
    loops = []
    prompt_output = []
    answers = iter(["2"] if has_existing_agent else ["2", "2"])
    inspection = "Inspecting the Ollama agent and servers (read-only checks)...\n"
    server_menu = (
        inspection + "   Ollama: endpoint [diagnostic]\n"
        "Ollama agent: /new/ollama-agent\n"
        "Choose Ollama server:\n"
        "  [1] first server\n"
        "  [2] second [server]\n"
    )
    selected_server = server_menu + "Selected endpoint: second [server]\n"
    model_menu = (
        selected_server + "   Ollama: model [diagnostic]\n"
        "Choose installed local tool-capable model:\n"
        "  [1] first-model\n"
        "  [2] second-model\n"
    )

    async def discover(received):
        assert received is request
        assert stream.getvalue() == inspection
        loops.append(asyncio.get_running_loop())
        return CliProviderDiscovery(command, endpoints, ("endpoint [diagnostic]",))

    async def list_models(received, endpoint):
        assert not has_existing_agent, "existing agents must skip model discovery"
        assert received is request
        assert endpoint is endpoints[1]
        assert loops[0].is_closed()
        assert stream.getvalue() == selected_server
        loops.append(asyncio.get_running_loop())
        return CliDiscoveredModels(
            ("first-model", "second-model"), ("model [diagnostic]",)
        )

    def prompt(message):
        assert message == "Choose a number (0 cancels): "
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        assert loops[-1].is_closed()
        prompt_output.append(stream.getvalue())
        return next(answers)

    onboarding = CliProviderOnboarding(discover, list_models)
    monkeypatch.setattr(
        plugin_setup, "canonicalize_provider_options", Mock(return_value={})
    )
    monkeypatch.setattr(
        plugin_setup,
        "verify_provider_options",
        Mock(
            return_value={
                "ollama": SimpleNamespace(
                    onboarding=onboarding, validate_request=Mock()
                )
            }
        ),
    )
    existing = (
        AgentConfig(
            cli_cmd=["/old/ollama-agent"],
            provider_kind="ollama",
            default_model="kept-model",
            environment={"KEEP_ME": "value", "OLLAMA_HOST": "http://old:11434"},
            invocation_timeout_seconds=47,
            extra_args=[],
            max_retries=2,
        )
        if has_existing_agent
        else None
    )
    original = existing.model_dump() if existing is not None else None

    result = plugin_setup.prepare_local_agent(request, console, prompt, existing)

    assert isinstance(result, AgentConfig)
    assert result is not existing
    assert result.environment is not endpoints[1].environment
    assert all(loop.is_closed() for loop in loops)
    assert next(answers, None) is None
    if existing is not None:
        assert existing.model_dump() == original
        assert result == existing.model_copy(
            update={
                "cli_cmd": list(command),
                "environment": {**existing.environment, **endpoints[1].environment},
            }
        )
        assert result.cli_cmd is not existing.cli_cmd
        assert result.environment is not existing.environment
        assert len(loops) == 1
        assert prompt_output == [server_menu]
        assert stream.getvalue().startswith(selected_server)
    else:
        assert result == AgentConfig(
            cli_cmd=list(command),
            provider_kind="ollama",
            default_model="second-model",
            environment=dict(endpoints[1].environment),
            invocation_timeout_seconds=None,
            invocation_idle_timeout_seconds=None,
        )
        assert len(loops) == 2
        assert loops[0] is not loops[1]
        assert prompt_output == [server_menu, model_menu]
        assert stream.getvalue().startswith(model_menu)
    output = stream.getvalue()
    assert "Inference endpoint: http://second:11434" in output
    assert "your user permissions" in output
    assert "credentials" in output
    assert "are not sandboxes" in output
