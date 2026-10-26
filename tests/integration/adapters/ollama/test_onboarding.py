import asyncio
import importlib
import io
import os
import signal
import sys
from collections import deque
from pathlib import Path

import httpx
import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from crewplane.cli.app import app
from crewplane.cli.onboarding import OnboardingOptions, run_onboarding, write_text_file
from crewplane.cli.project_init import initialize_project_templates
from crewplane.core.config import load_config
from crewplane.runtime.agent.process.drain import (
    ProcessDrainError,
    ProcessDrainEvidence,
    unconfirmed_process_cleanup,
)
from tests.helpers.cli_provider import (
    OLLAMA_PACKAGE,
    command_receipts,
    wait_for_receipt,
)
from tests.helpers.platforms import requires_posix
from tests.helpers.processes import kill_process_group


@pytest.fixture
def onboarding_project(ollama, discovery_server, tmp_path, monkeypatch):
    del ollama
    stream = io.StringIO()
    console = Console(file=stream, width=180, color_system=None)
    initialize_project_templates(console, tmp_path)
    platform = importlib.import_module("crewplane_ollama.discovery_platform")
    monkeypatch.setattr(platform.platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform.platform, "release", lambda: "linux")
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)

    def handler(request):
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "1"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "local-text"}]})
        return httpx.Response(200, json={"capabilities": ["completion", "tools"]})

    discovery_server(handler)
    return stream, console


def run_setup(root, console, executable, answers, write=write_text_file, commands=None):
    pending = deque(answers)
    lookup = commands if commands is not None else {"ollama-agent": str(executable)}
    run_onboarding(
        OnboardingOptions(
            root,
            console,
            True,
            True,
            pending.popleft,
            lookup.get,
            write,
        )
    )
    assert not pending


def test_onboarding_persists_endpoint_for_validation_and_later_run(
    ollama, onboarding_project, tmp_path, monkeypatch
):
    stream, console = onboarding_project
    monkeypatch.setenv("OLLAMA_HOST", "http://172.20.0.1:11434")
    run_setup(tmp_path, console, ollama.executable, ["1", "1", "1", "1", "y"])
    output = stream.getvalue()
    config = load_config(tmp_path / ".crewplane/config.yml")
    agent = config.agents["local"]
    assert agent.provider_kind == "ollama"
    assert agent.cli_cmd == [str(ollama.executable)]
    assert agent.default_model == "local-text"
    assert agent.environment == {"OLLAMA_HOST": "http://172.20.0.1:11434"}
    assert config.settings.integrations.invoker.options == ollama.options
    assert "Reply with OK only." in output
    assert "Onboarding complete" in output
    assert (
        "providers: [local]"
        in (tmp_path / ".crewplane/workflows/single-agent-review.task.md").read_text()
    )
    assert all(
        receipt["host"] == "http://172.20.0.1:11434"
        for receipt in command_receipts(ollama.receipts, "check")
    )

    monkeypatch.setenv("OLLAMA_HOST", "http://wrong-host:9")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        app,
        [
            "run",
            "--no-live",
            "--tasks",
            ".crewplane/workflows/example-templates/text-generation-example.task.md",
        ],
    )
    assert result.exit_code == 0, result.output
    assert all(
        receipt["host"] == "http://172.20.0.1:11434"
        for receipt in command_receipts(ollama.receipts, "check")
    )
    invocations = command_receipts(ollama.receipts, "run")
    assert len(invocations) == 2
    assert all(receipt["host"] == "http://172.20.0.1:11434" for receipt in invocations)
    assert os.environ["OLLAMA_HOST"] == "http://wrong-host:9"


def test_wsl_multiple_servers_require_selection(
    ollama, onboarding_project, tmp_path, monkeypatch
):
    stream, console = onboarding_project
    platform = importlib.import_module("crewplane_ollama.discovery_platform")
    monkeypatch.setattr(platform, "default_route_host", lambda: "172.20.0.1")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    run_setup(tmp_path, console, ollama.executable, ["1", "1", "2", "1", "y"])
    agent = load_config(tmp_path / ".crewplane/config.yml").agents["local"]
    assert agent.environment["OLLAMA_HOST"] == "http://172.20.0.1:11434"
    assert "[1] http://127.0.0.1:11434" in stream.getvalue()
    assert "[2] http://172.20.0.1:11434" in stream.getvalue()


@pytest.mark.parametrize(
    "answers",
    [["1", "1", "0", "2"], ["1", "1", "1", "0", "2"], ["1", "1", "1", "1", "n"]],
)
def test_cancel_or_decline_preserves_defaults(
    ollama, onboarding_project, tmp_path, answers
):
    stream, console = onboarding_project
    path = tmp_path / ".crewplane/config.yml"
    before = path.read_bytes()
    run_setup(tmp_path, console, ollama.executable, answers)
    assert path.read_bytes() == before
    assert "No files changed" in stream.getvalue()
    assert command_receipts(ollama.receipts, "check") == []


def test_no_models_does_not_write_config(
    ollama, onboarding_project, discovery_server, tmp_path
):
    stream, console = onboarding_project

    def handler(request):
        del request
        return httpx.Response(200, json={"version": "1", "models": []})

    discovery_server(handler)
    path = tmp_path / ".crewplane/config.yml"
    before = path.read_bytes()
    run_setup(tmp_path, console, ollama.executable, ["1", "1", "1", "1"])
    assert path.read_bytes() == before
    assert "No installed local tool-capable completion models" in stream.getvalue()


@pytest.mark.parametrize("failure", ["client", "server", "models"])
def test_ollama_setup_failure_can_continue_with_other_selected_provider(
    ollama, onboarding_project, discovery_server, tmp_path, failure
):
    stream, console = onboarding_project
    commands = {
        "codex": str(ollama.executable),
        "ollama-agent": str(ollama.executable),
    }
    if failure == "client":
        del commands["ollama-agent"]

    def handler(request):
        if failure == "server":
            return httpx.Response(503)
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "1"})
        return httpx.Response(200, json={"models": []})

    discovery_server(handler)
    answers = ["1", "1,2"]
    if failure == "models":
        answers.append("1")
    answers.extend(["1", "y"])
    run_setup(tmp_path, console, ollama.executable, answers, commands=commands)

    config = load_config(tmp_path / ".crewplane/config.yml")
    assert set(config.agents) == {"codex"}
    assert config.settings.integrations.invoker.options == {}
    assert (
        "providers: [codex]"
        in (tmp_path / ".crewplane/workflows/single-agent-review.task.md").read_text()
    )
    assert "Ollama was not configured." in stream.getvalue()
    assert "Onboarding configured: codex." in stream.getvalue()
    assert command_receipts(ollama.receipts, "check") == []


def test_cancelling_ollama_selection_can_return_to_other_providers(
    ollama, onboarding_project, tmp_path
):
    stream, console = onboarding_project
    run_setup(
        tmp_path,
        console,
        ollama.executable,
        ["1", "2", "0", "1", "1", "y"],
        commands={
            "codex": str(ollama.executable),
            "ollama-agent": str(ollama.executable),
        },
    )
    assert set(load_config(tmp_path / ".crewplane/config.yml").agents) == {"codex"}
    assert "Ollama setup was cancelled." in stream.getvalue()
    assert "Onboarding configured: codex." in stream.getvalue()
    assert command_receipts(ollama.receipts, "check") == []


def test_ollama_plugin_is_not_loaded_when_another_provider_is_selected(
    ollama, onboarding_project, tmp_path, monkeypatch
):
    stream, console = onboarding_project
    monkeypatch.setitem(sys.modules, "crewplane_ollama", None)
    run_setup(
        tmp_path,
        console,
        ollama.executable,
        ["1", "1", "y"],
        commands={
            "codex": str(ollama.executable),
            "ollama-agent": str(ollama.executable),
        },
    )
    assert "Onboarding configured: codex." in stream.getvalue()
    assert "Inspecting Ollama" not in stream.getvalue()
    assert command_receipts(ollama.receipts, "check") == []


@pytest.mark.parametrize("selection", ["1,2", "2"])
def test_failed_readiness_can_remove_ollama_and_finish_other_provider(
    ollama, onboarding_project, tmp_path, monkeypatch, selection
):
    stream, console = onboarding_project
    monkeypatch.setenv("CREWPLANE_FAKE_SCENARIO", "empty")
    run_setup(
        tmp_path,
        console,
        ollama.executable,
        ["1", selection, "1", "1", "y", "1"]
        + (["1"] if selection == "2" else [])
        + ["y"],
        commands={
            "codex": str(ollama.executable),
            "ollama-agent": str(ollama.executable),
        },
    )

    config = load_config(tmp_path / ".crewplane/config.yml")
    assert set(config.agents) == {"codex"}
    assert config.settings.integrations.invoker.options == {}
    assert (
        "providers: [codex]"
        in (tmp_path / ".crewplane/workflows/single-agent-review.task.md").read_text()
    )
    output = stream.getvalue()
    assert "readiness failed" in output
    assert "Replace the settings saved by this attempt to use codex" in output
    assert "Onboarding configured: codex." in output
    assert "Ollama is configured and passed" not in output
    assert len(command_receipts(ollama.receipts, "check")) == 1


def test_failed_ollama_only_readiness_can_restore_mock_settings(
    ollama, onboarding_project, tmp_path, monkeypatch
):
    stream, console = onboarding_project
    monkeypatch.setenv("CREWPLANE_FAKE_SCENARIO", "empty")
    files = [
        tmp_path / ".crewplane/config.yml",
        tmp_path / ".crewplane/workflows/single-agent-review.task.md",
    ]
    before = [path.read_bytes() for path in files]
    run_setup(
        tmp_path,
        console,
        ollama.executable,
        ["1", "1", "1", "1", "y", "1", "y"],
    )
    assert [path.read_bytes() for path in files] == before
    assert "Original mock settings restored" in stream.getvalue()
    assert "Onboarding complete." not in stream.getvalue()


@pytest.mark.parametrize("decision", [["2"], ["1", "n"]])
def test_declining_readiness_recovery_keeps_saved_settings(
    ollama, onboarding_project, tmp_path, monkeypatch, decision
):
    stream, console = onboarding_project
    monkeypatch.setenv("CREWPLANE_FAKE_SCENARIO", "empty")
    with pytest.raises(typer.Exit):
        run_setup(
            tmp_path,
            console,
            ollama.executable,
            ["1", "1,2", "1", "1", "y", *decision],
            commands={
                "codex": str(ollama.executable),
                "ollama-agent": str(ollama.executable),
            },
        )
    assert set(load_config(tmp_path / ".crewplane/config.yml").agents) == {
        "codex",
        "local",
    }
    assert "Files were not rolled back." in stream.getvalue()
    assert "Onboarding complete." not in stream.getvalue()


@pytest.mark.parametrize("edit", ["comment", "remove", "decode"])
def test_readiness_recovery_preserves_files_changed_after_saving(
    ollama, onboarding_project, tmp_path, monkeypatch, edit
):
    stream, console = onboarding_project
    monkeypatch.setenv("CREWPLANE_FAKE_SCENARIO", "empty")
    path = tmp_path / ".crewplane/config.yml"
    pending = deque(["1", "1,2", "1", "1", "y", "1", "edit-and-confirm"])
    changed = b"# User edit\n"

    def answer():
        value = pending.popleft()
        if value != "edit-and-confirm":
            return value
        if edit == "remove":
            path.unlink()
        else:
            path.write_bytes(changed if edit == "comment" else b"\xff")
        return "y"

    commands = {
        "codex": str(ollama.executable),
        "ollama-agent": str(ollama.executable),
    }
    with pytest.raises(typer.Exit):
        run_onboarding(
            OnboardingOptions(
                tmp_path, console, True, True, answer, commands.get, write_text_file
            )
        )
    assert not pending
    if edit == "remove":
        assert not path.exists()
    else:
        assert path.read_bytes() == (changed if edit == "comment" else b"\xff")
    assert "Onboarding will not rewrite this Crewplane setup." in stream.getvalue()
    assert "Onboarding complete." not in stream.getvalue()


@pytest.mark.parametrize("failed_file", ["config.yml", "single-agent-review.task.md"])
def test_readiness_recovery_reports_partial_writes(
    ollama, onboarding_project, tmp_path, monkeypatch, failed_file
):
    stream, console = onboarding_project
    monkeypatch.setenv("CREWPLANE_FAKE_SCENARIO", "empty")
    writes = {}

    def fail_replacement(path, content):
        writes[path.name] = writes.get(path.name, 0) + 1
        if path.name == failed_file and writes[path.name] == 2:
            raise OSError("replacement blocked")
        write_text_file(path, content)

    with pytest.raises(typer.Exit):
        run_setup(
            tmp_path,
            console,
            ollama.executable,
            ["1", "1,2", "1", "1", "y", "1", "y"],
            write=fail_replacement,
            commands={
                "codex": str(ollama.executable),
                "ollama-agent": str(ollama.executable),
            },
        )

    output = stream.getvalue()
    assert "replacement blocked" in output
    assert "Partial onboarding update." in output
    assert "Onboarding complete." not in output
    config = load_config(tmp_path / ".crewplane/config.yml")
    assert ("local" in config.agents) == (failed_file == "config.yml")
    workflow = (
        tmp_path / ".crewplane/workflows/single-agent-review.task.md"
    ).read_text()
    assert ("local" in workflow) == (failed_file == "single-agent-review.task.md")
    assert len(command_receipts(ollama.receipts, "check")) == 1


def test_readiness_cleanup_failure_is_not_offered_as_optional_recovery(
    ollama, onboarding_project, tmp_path, monkeypatch
):
    stream, console = onboarding_project
    availability = importlib.import_module("crewplane_ollama.availability")
    cleanup_error = ProcessDrainError(
        ProcessDrainEvidence(42101, 42102, True, False), "probe cleanup unconfirmed"
    )

    async def failed_probe(request, command):
        del request, command
        raise cleanup_error

    monkeypatch.setattr(availability, "_probe_command", failed_probe)
    with pytest.raises(ProcessDrainError) as caught:
        run_setup(tmp_path, console, ollama.executable, ["1", "1", "1", "1", "y"])

    assert caught.value is cleanup_error
    assert "local" in load_config(tmp_path / ".crewplane/config.yml").agents
    assert "Continue onboarding without Ollama" not in stream.getvalue()
    assert "Onboarding complete." not in stream.getvalue()


def test_edited_files_skip_plugin_discovery(
    ollama, onboarding_project, tmp_path, monkeypatch
):
    _, console = onboarding_project
    module = importlib.import_module("crewplane_ollama.discovery")

    def forbidden(*args):
        del args
        pytest.fail("edited projects must not start discovery")

    monkeypatch.setattr(module, "run_discovery", forbidden)
    path = tmp_path / ".crewplane/config.yml"
    edited = path.read_text() + "\n# user edit\n"
    path.write_text(edited)
    run_setup(tmp_path, console, ollama.executable, [])
    assert path.read_text() == edited


def test_custom_discovery_budget_reaches_both_phases(
    ollama, onboarding_project, tmp_path, monkeypatch
):
    stream, console = onboarding_project
    module = importlib.import_module("crewplane_ollama.discovery")
    query = module.run_discovery
    received = []

    async def record(task, root):
        received.append((task.operation, task.timeout_seconds))
        return await query(task, root)

    monkeypatch.setattr(module, "run_discovery", record)
    answers = deque(["1", "1", "1", "0", "2"])
    run_onboarding(
        OnboardingOptions(
            tmp_path,
            console,
            True,
            True,
            answers.popleft,
            {"ollama-agent": str(ollama.executable)}.get,
            write_text_file,
            discovery_timeout_seconds=60,
        )
    )
    assert not answers
    assert received == [("endpoints", 60), ("models", 60)]
    assert "No files changed" in stream.getvalue()


@pytest.mark.parametrize("phase", ["endpoints", "models"])
def test_discovery_cleanup_failure_is_not_swallowed(
    ollama, onboarding_project, tmp_path, monkeypatch, phase
):
    stream, console = onboarding_project
    module = importlib.import_module("crewplane_ollama.discovery")
    query = module.run_discovery
    cleanup_error = ProcessDrainError(
        ProcessDrainEvidence(42101, 42102, True, False), "worker cleanup unconfirmed"
    )

    async def failed_discovery(task, root):
        if task.operation == phase:
            try:
                raise asyncio.CancelledError from cleanup_error
            except asyncio.CancelledError as exc:
                raise TimeoutError from exc
        return await query(task, root)

    monkeypatch.setattr(module, "run_discovery", failed_discovery)
    paths = (
        tmp_path / ".crewplane/config.yml",
        tmp_path / ".crewplane/workflows/single-agent-review.task.md",
    )
    before = [path.read_bytes() for path in paths]
    answers = ["1", "1"] + (["1"] if phase == "models" else [])
    with pytest.raises(TimeoutError) as caught:
        run_setup(tmp_path, console, ollama.executable, answers)
    assert unconfirmed_process_cleanup(caught.value) is cleanup_error
    assert [path.read_bytes() for path in paths] == before
    assert "No files changed" not in stream.getvalue()
    assert command_receipts(ollama.receipts, "check") == []


@requires_posix
@pytest.mark.race
@pytest.mark.parametrize("phase", ["server", "model"])
def test_selection_exits_on_first_sigint(ollama, onboarding_project, tmp_path, phase):
    del onboarding_project
    fixture = (
        Path(__file__).resolve().parents[3]
        / "fixtures/providers/ollama_onboarding_prompt.py"
    )
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(OLLAMA_PACKAGE / "src"), str(tmp_path)]),
    }
    paths = (
        tmp_path / ".crewplane/config.yml",
        tmp_path / ".crewplane/workflows/single-agent-review.task.md",
    )
    before = [path.read_bytes() for path in paths]

    async def scenario():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(fixture),
            phase,
            str(ollama.executable),
            cwd=tmp_path,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            await wait_for_receipt(tmp_path, "prompt-ready")
            process.send_signal(signal.SIGINT)
            # Keep stdin open so EOF cannot masquerade as a successful interrupt.
            await asyncio.wait_for(process.wait(), 5)
            stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
            assert process.returncode == 130, (stdout, stderr)
            expected = (
                "Choose Ollama server:"
                if phase == "server"
                else "Choose installed local tool-capable model:"
            )
            assert expected in stdout.decode()
        finally:
            if process.returncode is None:
                kill_process_group(process.pid)
                await asyncio.wait_for(process.communicate(), 5)

    asyncio.run(scenario())
    assert [path.read_bytes() for path in paths] == before
    assert command_receipts(ollama.receipts, "check") == []


def test_new_profile_discloses_host_authority_and_endpoint_before_write(
    ollama, onboarding_project, tmp_path
):
    stream, console = onboarding_project
    writes = []

    def write(path, content):
        output = " ".join(stream.getvalue().split())
        assert "Inference endpoint: http://127.0.0.1:11434" in output
        assert "your user permissions" in output
        assert "reachable files, credentials, and networks" in output
        assert "are not sandboxes" in output
        writes.append(path)
        write_text_file(path, content)

    run_setup(
        tmp_path, console, ollama.executable, ["1", "1", "1", "1", "y"], write=write
    )
    assert len(writes) == 2
    agent = load_config(tmp_path / ".crewplane/config.yml").agents["local"]
    assert agent.invocation_timeout_seconds is None
    assert agent.invocation_idle_timeout_seconds is None
    assert agent.extra_args == []
    assert agent.max_retries == 0
