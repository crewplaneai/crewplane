import importlib
import io
import shutil
from collections import deque
from importlib import metadata
from textwrap import dedent

import httpx
import pytest
import yaml
from rich.console import Console

from crewplane.cli.onboarding import OnboardingOptions, run_onboarding, write_text_file
from crewplane.cli.onboarding.rendering import (
    render_provider_ready_config,
    rendered_default_config,
)
from crewplane.core.config import load_config
from tests.helpers.cli_provider import command_receipts


@pytest.fixture
def configured_project(ollama, discovery_server, tmp_path, monkeypatch):
    del ollama
    directory = tmp_path / ".crewplane"
    directory.mkdir()
    config = directory / "config.yml"
    config.write_text(
        dedent("""\
        version: "1.0"
        # Keep project settings and provider comments.
        agents:
          codex:
            cli_cmd: [missing-codex, exec]
            provider_kind: codex
          local:
            cli_cmd: [old-ollama-agent]
            provider_kind: ollama
            default_model: kept-model
            invocation_timeout_seconds: 47
            extra_args: []
            environment:
              KEEP_ME: existing-value
              OLLAMA_HOST: http://old-host:11434
        settings:
          integrations:
            invoker:
              implementation: cli
              options:
                providers:
                  custom:
                    implementation: custom_provider:CAPABILITY
                    distribution: custom-provider
        """),
        encoding="utf-8",
    )
    workflows = directory / "workflows"
    workflows.mkdir()
    workflow = workflows / "custom.task.md"
    workflow.write_bytes(b"This workflow must not be read or rewritten.\n")
    platform = importlib.import_module("crewplane_ollama.discovery_platform")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.setattr(platform.platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "default_route_host", lambda: "172.20.0.1")

    def handler(request):
        if request.url.host == "127.0.0.1":
            raise httpx.ConnectError("No WSL server", request=request)
        return httpx.Response(200, json={"version": "1"})

    discovery_server(handler)
    stream = io.StringIO()
    console = Console(file=stream, width=180, color_system=None)
    return config, workflow, stream, console


def configure_connection(root, console, commands, answers, write=write_text_file):
    pending = deque(answers)
    run_onboarding(
        OnboardingOptions(
            root, console, True, True, pending.popleft, commands.get, write
        )
    )
    assert not pending


def test_existing_project_saves_wsl_connection_without_changing_workflows(
    ollama, configured_project, tmp_path
):
    config_path, workflow, stream, console = configured_project
    before = load_config(config_path)
    original_workflow = workflow.read_bytes()
    configure_connection(
        tmp_path,
        console,
        {"ollama-agent": str(ollama.executable)},
        ["y", "1", "y"],
    )

    after = load_config(config_path)
    assert after.agents["local"] == before.agents["local"].model_copy(
        update={
            "cli_cmd": [str(ollama.executable)],
            "environment": {
                "KEEP_ME": "existing-value",
                "OLLAMA_HOST": "http://172.20.0.1:11434",
            },
        }
    )
    assert after.agents["codex"] == before.agents["codex"]
    providers = after.settings.integrations.invoker.options["providers"]
    assert (
        providers["custom"]
        == before.settings.integrations.invoker.options["providers"]["custom"]
    )
    assert providers["ollama"] == ollama.options["providers"]["ollama"]
    assert "# Keep project settings and provider comments." in config_path.read_text()
    assert workflow.read_bytes() == original_workflow
    assert list(workflow.parent.iterdir()) == [workflow]
    assert command_receipts(ollama.receipts, "check") == []
    assert "Ollama connection saved" in stream.getvalue()


@pytest.mark.parametrize("command_kind", ["absolute", "relative", "agent-path"])
def test_connection_uses_configured_executable_outside_terminal_path(
    ollama, configured_project, tmp_path, monkeypatch, command_kind
):
    config_path, _, stream, console = configured_project
    config = load_config(config_path)
    agent = config.agents["local"]
    agent.cli_cmd[0] = str(ollama.executable)
    if command_kind == "relative":
        agent.cli_cmd[0] = f"./{ollama.executable.name}"
    elif command_kind == "agent-path":
        agent.cli_cmd[0] = ollama.executable.name
        agent.environment["PATH"] = "."
    config_path.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path.parent)
    monkeypatch.setenv("PATH", "")
    assert shutil.which("ollama-agent") is None
    answers = deque(["y", "1", "y"])

    run_onboarding(
        OnboardingOptions(
            tmp_path,
            console,
            True,
            True,
            answers.popleft,
            shutil.which,
            write_text_file,
        )
    )

    assert "Ollama connection saved" in stream.getvalue()
    assert not answers
    after = load_config(config_path).agents["local"]
    assert after == agent.model_copy(
        update={
            "cli_cmd": [str(ollama.executable)],
            "environment": {
                **agent.environment,
                "OLLAMA_HOST": "http://172.20.0.1:11434",
            },
        }
    )


@pytest.mark.parametrize("failure", ["client", "server"])
def test_missing_ollama_leaves_existing_project_usable(
    ollama, configured_project, discovery_server, tmp_path, failure
):
    config_path, workflow, stream, console = configured_project
    before = config_path.read_bytes(), workflow.read_bytes()
    commands = {"ollama-agent": str(ollama.executable)} if failure != "client" else {}
    if failure == "server":

        def unavailable(request):
            del request
            return httpx.Response(503)

        discovery_server(unavailable)
    configure_connection(tmp_path, console, commands, ["y"])

    assert (config_path.read_bytes(), workflow.read_bytes()) == before
    assert "Ollama was not configured" in stream.getvalue()
    assert command_receipts(ollama.receipts, "check") == []


@pytest.mark.parametrize("answers", [["n"], ["y", "0"], ["y", "1", "n"]])
def test_declining_connection_setup_preserves_existing_files(
    ollama, configured_project, tmp_path, answers
):
    config_path, workflow, _, console = configured_project
    before = config_path.read_bytes(), workflow.read_bytes()
    configure_connection(
        tmp_path, console, {"ollama-agent": str(ollama.executable)}, answers
    )
    assert (config_path.read_bytes(), workflow.read_bytes()) == before


def test_existing_project_can_add_local_agent(
    ollama, configured_project, discovery_server, tmp_path
):
    config_path, workflow, stream, console = configured_project
    config_path.write_text(
        render_provider_ready_config(rendered_default_config(), ("codex",)),
        encoding="utf-8",
    )
    before = load_config(config_path)
    original_workflow = workflow.read_bytes()

    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "local-text"}]})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion", "tools"]})
        if request.url.host == "127.0.0.1":
            raise httpx.ConnectError("No WSL server", request=request)
        return httpx.Response(200, json={"version": "1"})

    discovery_server(handler)
    configure_connection(
        tmp_path,
        console,
        {"ollama-agent": str(ollama.executable)},
        ["y", "1", "1", "y"],
    )

    after = load_config(config_path)
    assert after.agents["codex"] == before.agents["codex"]
    assert after.agents["local"].default_model == "local-text"
    assert after.agents["local"].cli_cmd == [str(ollama.executable)]
    assert after.agents["local"].environment == {
        "OLLAMA_HOST": "http://172.20.0.1:11434"
    }
    assert after.settings.integrations.invoker.options == ollama.options
    assert workflow.read_bytes() == original_workflow
    assert list(workflow.parent.iterdir()) == [workflow]
    assert "Ollama connection saved" in stream.getvalue()
    assert command_receipts(ollama.receipts, "check") == []


def test_missing_plugin_skips_optional_connection_setup(
    configured_project, tmp_path, monkeypatch
):
    config_path, workflow, stream, console = configured_project
    before = config_path.read_bytes(), workflow.read_bytes()
    version = metadata.version

    def without_ollama(name):
        if name == "crewplane-ollama":
            raise metadata.PackageNotFoundError(name)
        return version(name)

    monkeypatch.setattr(metadata, "version", without_ollama)
    configure_connection(tmp_path, console, {}, [])

    assert (config_path.read_bytes(), workflow.read_bytes()) == before
    assert list(workflow.parent.iterdir()) == [workflow]
    assert "Configure the Ollama connection?" not in stream.getvalue()


@pytest.mark.parametrize("failure", ["write", "concurrent_edit"])
def test_failed_save_preserves_config_and_cleans_temporary_file(
    ollama, configured_project, tmp_path, failure
):
    config_path, workflow, stream, console = configured_project
    original = config_path.read_bytes()
    original_workflow = workflow.read_bytes()
    expected = original if failure == "write" else original + b"# User edit\n"

    def write(path, content):
        if failure == "write":
            path.write_text("partial", encoding="utf-8")
            raise OSError("Disk full")
        write_text_file(path, content)
        config_path.write_bytes(expected)

    configure_connection(
        tmp_path,
        console,
        {"ollama-agent": str(ollama.executable)},
        ["y", "1", "y"],
        write,
    )

    assert config_path.read_bytes() == expected
    assert workflow.read_bytes() == original_workflow
    assert list(config_path.parent.glob(".ollama-*")) == []
    assert "Ollama connection saved" not in stream.getvalue()
    assert "Ollama was not configured" in stream.getvalue()


def test_shared_command_anchor_reports_failure_without_changing_files(
    ollama, configured_project, tmp_path
):
    config_path, workflow, stream, console = configured_project
    config_path.write_text(
        dedent("""\
        version: "1.0"
        agents:
          local:
            cli_cmd: &shared_command [old-ollama-agent]
            provider_kind: ollama
            default_model: kept-model
            invocation_timeout_seconds: 47
          backup:
            cli_cmd: *shared_command
            provider_kind: ollama
            default_model: kept-model
        """),
        encoding="utf-8",
    )
    before = config_path.read_bytes(), workflow.read_bytes()

    configure_connection(
        tmp_path,
        console,
        {"ollama-agent": str(ollama.executable)},
        ["y", "1"],
    )

    assert (config_path.read_bytes(), workflow.read_bytes()) == before
    assert list(config_path.parent.glob(".ollama-*")) == []
    assert "undefined alias 'shared_command'" in stream.getvalue()
    assert "Ollama was not configured" in stream.getvalue()
    assert "Ollama connection saved" not in stream.getvalue()


@pytest.mark.race
def test_invoker_change_during_confirmation_preserves_config(
    ollama, configured_project, tmp_path
):
    config_path, workflow, stream, console = configured_project
    updated_config = load_config(config_path).model_dump(mode="json")
    updated_config["settings"]["integrations"]["invoker"] = {"implementation": "mock"}
    updated = yaml.safe_dump(updated_config, sort_keys=False)
    original_workflow = workflow.read_bytes()
    answers = deque(["y", "1", "y"])

    def read_input():
        if len(answers) == 3:
            config_path.write_text(updated, encoding="utf-8")
        return answers.popleft()

    run_onboarding(
        OnboardingOptions(
            tmp_path,
            console,
            True,
            True,
            read_input,
            {"ollama-agent": str(ollama.executable)}.get,
            write_text_file,
        )
    )

    assert config_path.read_text(encoding="utf-8") == updated
    assert workflow.read_bytes() == original_workflow
    assert "active invoker changed" in stream.getvalue()
    assert "Ollama connection saved" not in stream.getvalue()


def test_connection_setup_does_not_replace_another_local_provider(
    configured_project, tmp_path
):
    config_path, workflow, stream, console = configured_project
    original = config_path.read_text().replace(
        "provider_kind: ollama", "provider_kind: codex"
    )
    config_path.write_text(original, encoding="utf-8")
    original_workflow = workflow.read_bytes()

    configure_connection(tmp_path, console, {}, ["y"])

    assert config_path.read_text() == original
    assert workflow.read_bytes() == original_workflow
    assert "already belongs to another provider" in stream.getvalue()


@pytest.mark.parametrize(
    "changes",
    [
        {"cli_cmd": ["ollama", "run", "--keepalive", "5m"]},
        {"extra_args": ["--verbose"]},
        {"invocation_timeout_seconds": None},
        {"prompt_transport": "argv", "prompt_transport_arg": "--prompt"},
        {"prompt_transport_arg": "--prompt"},
    ],
)
def test_legacy_profile_is_not_silently_migrated(
    ollama, configured_project, tmp_path, monkeypatch, changes
):
    config_path, workflow, stream, console = configured_project
    config = load_config(config_path)
    config.agents["local"] = config.agents["local"].model_copy(update=changes)
    config_path.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)
    )
    before = config_path.read_bytes(), workflow.read_bytes()
    discovery = importlib.import_module("crewplane_ollama.discovery")

    async def forbidden(*args):
        del args
        pytest.fail("legacy profiles must fail structural validation before discovery")

    monkeypatch.setattr(discovery, "run_discovery", forbidden)
    configure_connection(
        tmp_path, console, {"ollama-agent": str(ollama.executable)}, ["y"]
    )

    assert (config_path.read_bytes(), workflow.read_bytes()) == before
    assert "Regenerate or deliberately reconfigure" in stream.getvalue()
    assert "Ollama connection saved" not in stream.getvalue()
    assert not list(ollama.receipts.iterdir())


def test_connection_discloses_host_authority_and_endpoint_before_save(
    ollama, configured_project, tmp_path
):
    _, _, stream, console = configured_project
    writes = []

    def write(path, content):
        output = " ".join(stream.getvalue().split())
        assert "Inference endpoint: http://172.20.0.1:11434" in output
        assert "your user permissions" in output
        assert "credentials" in output
        assert "context is sent to this server" in output
        assert "are not sandboxes" in output
        writes.append(path)
        write_text_file(path, content)

    configure_connection(
        tmp_path,
        console,
        {"ollama-agent": str(ollama.executable)},
        ["y", "1", "y"],
        write,
    )
    assert len(writes) == 1


def test_connection_preserves_workflow_only_model_selection(
    ollama, configured_project, tmp_path
):
    config_path, _, _, console = configured_project
    config = load_config(config_path)
    config.agents["local"].default_model = None
    config_path.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)
    )
    configure_connection(
        tmp_path,
        console,
        {"ollama-agent": str(ollama.executable)},
        ["y", "1", "y"],
    )
    assert load_config(config_path).agents["local"].default_model is None


def test_discovered_command_is_validated_in_full_before_refresh(
    ollama, configured_project, tmp_path, monkeypatch
):
    config_path, workflow, stream, console = configured_project
    before = config_path.read_bytes(), workflow.read_bytes()
    discovery = importlib.import_module("crewplane_ollama.discovery")

    def legacy_discovery(request):
        del request
        return (str(ollama.executable), "run")

    monkeypatch.setattr(discovery, "discover_client", legacy_discovery)
    configure_connection(
        tmp_path,
        console,
        {"ollama-agent": str(ollama.executable)},
        ["y", "1"],
    )
    assert (config_path.read_bytes(), workflow.read_bytes()) == before
    assert "exactly one executable" in stream.getvalue()
    assert "Ollama connection saved" not in stream.getvalue()
