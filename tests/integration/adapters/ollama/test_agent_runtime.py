import asyncio
import json
import os
import sys
from threading import Event

import pytest

from crewplane.cli.run.preflight import compile_workflow_preview
from tests.helpers.cli_provider import (
    provider_source,
    quiet_console,
)
from tests.helpers.ollama_agent import (
    ScriptedOllama,
    command_call,
    final_response,
    ollama_server,
    tool_response,
)
from tests.helpers.ollama_agent_workflow import configured_agent

pytestmark = pytest.mark.usefixtures("ollama_package")


@pytest.mark.race
@pytest.mark.parametrize("stop", ["cancel", "timeout"])
@pytest.mark.parametrize("invocation_timeout", [None, 30])
def test_real_agent_http_wait_cancellation_has_no_published_output(
    tmp_path, stop, invocation_timeout
):
    started, release = Event(), Event()

    def handler(path, payload):
        del payload
        if path == "/api/show":
            return {"capabilities": ["completion", "tools"]}
        started.set()
        assert release.wait(20), "test did not release server worker"
        return final_response("too late to publish")

    with ollama_server(handler) as endpoint:
        agent, _, invoker = configured_agent(tmp_path, endpoint)
        agent.invocation_timeout_seconds = invocation_timeout
        output = tmp_path / "answer.md"

        async def run():
            task = asyncio.create_task(
                invoker.invoke(agent, "test-model", "wait for HTTP", output, tmp_path)
            )
            try:
                assert await asyncio.to_thread(started.wait, 10)
                if stop == "cancel":
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 10)
                else:
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(task, 0.05)
                assert not output.exists()
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        asyncio.run(run())


@pytest.mark.parametrize("invocation_timeout", [None, 21600])
def test_real_agent_completes_with_independent_timeouts(tmp_path, invocation_timeout):
    script = ScriptedOllama([final_response("Completed local task.")])
    with ollama_server(script) as endpoint:
        agent, _, invoker = configured_agent(tmp_path, endpoint)
        agent.invocation_timeout_seconds = invocation_timeout
        settings_path = tmp_path / ".crewplane" / "ollama.yml"
        settings_path.parent.mkdir(exist_ok=True)
        settings_path.write_text("readiness_timeout_seconds: 0.001\n")
        output = tmp_path / "answer.md"

        async def run():
            await asyncio.wait_for(
                invoker.invoke(agent, "test-model", "local task", output, tmp_path),
                10,
            )

        asyncio.run(run())
    assert output.read_text() == "Completed local task."
    assert len(script.requests) == 1


def test_project_root_retry_retains_edits_but_not_conversation(tmp_path):
    def fail(payload):
        assert json.loads(payload["messages"][-1]["content"])["exit_code"] == 0
        return 503, b"fail after effect"

    def restarted(payload):
        assert len(payload["messages"]) == 2
        return tool_response(
            command_call(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; print(Path('retained').read_text())",
                ]
            )
        )

    def finish(payload):
        assert (
            json.loads(payload["messages"][-1]["content"])["stdout"]
            == "edit persists\n"
        )
        return final_response(
            "Retried with a fresh conversation; previous project edit persists."
        )

    script = ScriptedOllama(
        [
            tool_response(
                command_call(
                    [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; Path('retained').write_text('edit persists')",
                    ]
                )
            ),
            fail,
            restarted,
            finish,
        ]
    )
    with ollama_server(script) as endpoint:
        agent, _, invoker = configured_agent(tmp_path, endpoint)
        agent.max_retries = 1
        agent.retry_delay_seconds = 0
        agent.retry_on_exit_codes = [1]
        asyncio.run(
            invoker.invoke(agent, "test-model", "task", tmp_path / "answer", tmp_path)
        )
    assert (tmp_path / "retained").read_text() == "edit persists"
    assert "fresh conversation" in (tmp_path / "answer").read_text()
    assert len(script.shows) == 2


@pytest.mark.parametrize("initial_host", [None, "http://inherited:11434"])
@pytest.mark.parametrize("changed", ["command", "endpoint", "version"])
def test_agent_command_endpoint_and_plugin_version_change_existing_signature(
    tmp_path, changed, initial_host, monkeypatch
):
    if initial_host is None:
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
    else:
        monkeypatch.setenv("OLLAMA_HOST", initial_host)
    agent, config, _ = configured_agent(tmp_path, "http://127.0.0.1:11434")

    def signature():
        result = compile_workflow_preview(
            config,
            provider_source(),
            quiet_console(),
            True,
            "read_only",
            project_root=tmp_path,
        )
        assert not result.has_errors()
        assert not (tmp_path / ".crewplane").exists()
        return result.workflow_signature

    before = signature()
    if changed == "command":
        agent.cli_cmd = [str(tmp_path / "other-agent")]
    elif changed == "endpoint":
        agent.environment["OLLAMA_HOST"] = "http://other-host:11434"
    else:
        metadata = next(tmp_path.glob("crewplane_ollama-*.dist-info/METADATA"))
        metadata.write_text("Name: crewplane-ollama\nVersion: 0.3.0\n")
    assert signature() != before
    parent_host = os.environ.get("OLLAMA_HOST")
    assert parent_host == initial_host
