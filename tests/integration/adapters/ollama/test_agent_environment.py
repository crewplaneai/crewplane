import asyncio
import os

from crewplane.adapters.invokers.cli import CliInvokerAdapter
from crewplane.bootstrap import build_runtime_config_snapshot
from crewplane.core.config import AgentConfig
from tests.helpers.cli_provider import (
    command_receipts,
    provider_config,
    provider_source,
    quiet_console,
)


def test_configured_path_and_host_apply_to_preflight_and_execution(
    ollama, tmp_path, monkeypatch
):
    monkeypatch.setenv("PATH", "")
    agent = AgentConfig(
        cli_cmd=["ollama-agent"],
        provider_kind="ollama",
        invocation_timeout_seconds=60,
        default_model="text",
        environment={"PATH": ".", "OLLAMA_HOST": "http://configured:11434"},
    )
    config = provider_config(agent, ollama.options)
    canonical = build_runtime_config_snapshot(
        config, quiet_console(), True
    ).invoker_options
    adapter = CliInvokerAdapter()
    errors = asyncio.run(
        adapter.collect_availability_errors(
            provider_source().workflow, config, tmp_path, options=canonical
        )
    )
    assert errors == ()
    invoker = adapter.create_invoker(config, canonical)
    asyncio.run(
        invoker.invoke(
            agent, "text", "supplied content", tmp_path / "answer.md", tmp_path
        )
    )
    assert "supplied content" in (tmp_path / "answer.md").read_text()
    receipts = command_receipts(ollama.receipts, "run") + command_receipts(
        ollama.receipts, "check"
    )
    assert len(receipts) == 2
    assert all(receipt["host"] == "http://configured:11434" for receipt in receipts)
    assert os.environ["PATH"] == ""


def test_overlapping_agents_keep_distinct_environments(ollama, tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://inherited:11434")
    agents = [
        AgentConfig(
            cli_cmd=[str(ollama.executable)],
            provider_kind="ollama",
            invocation_timeout_seconds=60,
            environment={"OLLAMA_HOST": f"http://server-{index}:11434"},
        )
        for index in range(2)
    ]
    config = provider_config(agents[0], ollama.options)
    canonical = build_runtime_config_snapshot(
        config, quiet_console(), True
    ).invoker_options
    invoker = CliInvokerAdapter().create_invoker(config, canonical)

    async def run():
        await asyncio.gather(
            *(
                invoker.invoke(
                    agent, "text", "prompt", tmp_path / f"answer-{index}.md", tmp_path
                )
                for index, agent in enumerate(agents)
            )
        )

    asyncio.run(run())
    assert sorted(
        receipt["host"] for receipt in command_receipts(ollama.receipts, "run")
    ) == ["http://server-0:11434", "http://server-1:11434"]
    assert os.environ["OLLAMA_HOST"] == "http://inherited:11434"
