import asyncio
import os

import pytest

from crewplane.adapters.invokers.cli import CliInvokerAdapter
from crewplane.bootstrap import build_runtime_config_snapshot
from crewplane.core.config import AgentConfig
from tests.helpers.cli_provider import (
    command_receipts,
    provider_config,
    provider_source,
    quiet_console,
)


@pytest.mark.parametrize(
    ("initial_host", "override", "expected_host"),
    [
        (None, None, None),
        ("http://inherited:11434", None, "http://inherited:11434"),
        (None, "http://configured:11434", "http://configured:11434"),
        (
            "http://inherited:11434",
            "http://configured:11434",
            "http://configured:11434",
        ),
    ],
)
def test_configured_path_and_host_apply_to_preflight_and_execution(
    ollama, tmp_path, monkeypatch, initial_host, override, expected_host
):
    if initial_host is None:
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
    else:
        monkeypatch.setenv("OLLAMA_HOST", initial_host)
    environment = {"PATH": "."}
    if override is not None:
        environment["OLLAMA_HOST"] = override
    monkeypatch.setenv("PATH", "")
    agent = AgentConfig(
        cli_cmd=["ollama-agent"],
        provider_kind="ollama",
        invocation_timeout_seconds=60,
        default_model="text",
        environment=environment,
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
    assert all(receipt["host"] == expected_host for receipt in receipts)
    parent_path = os.environ["PATH"]
    parent_host = os.environ.get("OLLAMA_HOST")
    assert parent_path == ""
    assert parent_host == initial_host


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
    parent_host = os.environ["OLLAMA_HOST"]
    assert parent_host == "http://inherited:11434"
