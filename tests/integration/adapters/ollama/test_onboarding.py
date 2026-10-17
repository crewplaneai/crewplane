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
        return httpx.Response(200, json={"capabilities": ["completion"]})

    discovery_server(handler)
    return stream, console


def run_setup(root, console, executable, answers, write=write_text_file):
    pending = deque(answers)
    run_onboarding(
        OnboardingOptions(
            root,
            console,
            True,
            True,
            pending.popleft,
            {"ollama": str(executable)}.get,
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
    assert agent.cli_cmd == [str(ollama.executable), "run"]
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
        for receipt in command_receipts(ollama.receipts, "run")
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
        for receipt in command_receipts(ollama.receipts, "run")
    )
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
    "answers", [["1", "1", "0"], ["1", "1", "1", "0"], ["1", "1", "1", "1", "n"]]
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
    assert command_receipts(ollama.receipts, "run") == []


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
    run_setup(tmp_path, console, ollama.executable, ["1", "1", "1"])
    assert path.read_bytes() == before
    assert "No installed local text-generation models" in stream.getvalue()


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
    answers = deque(["1", "1", "1", "0"])
    run_onboarding(
        OnboardingOptions(
            tmp_path,
            console,
            True,
            True,
            answers.popleft,
            {"ollama": str(ollama.executable)}.get,
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
    assert command_receipts(ollama.receipts, "run") == []


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
                else "Choose installed local text model:"
            )
            assert expected in stdout.decode()
        finally:
            if process.returncode is None:
                kill_process_group(process.pid)
                await asyncio.wait_for(process.communicate(), 5)

    asyncio.run(scenario())
    assert [path.read_bytes() for path in paths] == before
    assert command_receipts(ollama.receipts, "run") == []
