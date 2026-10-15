import asyncio
import importlib
import os
from dataclasses import replace

import pytest

from crewplane.adapters.invokers.cli import CliInvokerAdapter
from crewplane.adapters.invokers.cli_invoker import CliCommand, capabilities
from crewplane.core.config import AgentConfig
from tests.helpers.cli_provider import (
    FIXTURE_PACKAGE,
    expose_distribution,
    fake_ollama,
    fixture_options,
    provider_config,
    wait_for_receipt,
)
from tests.helpers.platforms import requires_posix


@pytest.fixture
def allocated_provider(tmp_path, monkeypatch):
    expose_distribution(
        monkeypatch, tmp_path, FIXTURE_PACKAGE, "crewplane-fixture-provider", "1.2.3"
    )
    module = importlib.import_module("crewplane_fixture_provider")
    executable, receipts = fake_ollama(monkeypatch, tmp_path)

    def build(request, prompt):
        owned = tmp_path / f"{prompt}.owned"
        owned.write_text("reserved")
        return CliCommand(list(request.config.cli_cmd), prompt.encode(), owned)

    monkeypatch.setattr(module, "FIXTURE", replace(module.FIXTURE, build_command=build))
    agent = AgentConfig(cli_cmd=[str(executable), "run"], provider_kind="fixture")
    config = provider_config(agent, fixture_options())
    adapter = CliInvokerAdapter()
    canonical = adapter.canonicalize_options("cli", "cli", fixture_options()).options
    return agent, adapter.create_invoker(config, canonical), receipts


@pytest.mark.parametrize("scenario", ["success", "partial"])
def test_runtime_releases_transferred_files(
    allocated_provider, tmp_path, monkeypatch, scenario
):
    agent, invoker, _ = allocated_provider
    monkeypatch.setenv("CREWPLANE_FAKE_SCENARIO", scenario)
    pending = invoker.invoke(agent, None, "request", tmp_path / "out", tmp_path)
    if scenario == "partial":
        with pytest.raises(RuntimeError):
            asyncio.run(pending)
        assert not (tmp_path / "out").exists()
    else:
        asyncio.run(pending)
        assert (tmp_path / "out").read_text().strip() == "answer: request"
    assert not (tmp_path / "request.owned").exists()


def test_planning_failure_releases_plugin_allocation(
    allocated_provider, tmp_path, monkeypatch
):
    agent, invoker, _ = allocated_provider

    def fail_header(**kwargs):
        del kwargs
        raise RuntimeError("plan assembly failed")

    monkeypatch.setattr(capabilities, "build_provider_log_header", fail_header)
    with pytest.raises(RuntimeError, match="plan assembly"):
        asyncio.run(invoker.invoke(agent, None, "request", tmp_path / "out", tmp_path))
    assert not (tmp_path / "request.owned").exists()


@requires_posix
@pytest.mark.parametrize(
    "timeout", ["invocation_timeout_seconds", "invocation_idle_timeout_seconds"]
)
def test_plugin_uses_existing_timeouts_and_process_cleanup(
    allocated_provider, tmp_path, monkeypatch, timeout
):
    agent, invoker, receipts = allocated_provider
    monkeypatch.setenv("CREWPLANE_FAKE_SCENARIO", "hold-run")
    setattr(agent, timeout, 0.4)
    with pytest.raises(RuntimeError, match="(?i)timed out|timeout|produced no output"):
        asyncio.run(invoker.invoke(agent, None, "request", tmp_path / "out", tmp_path))
    ready = next(receipts.glob("ready-*"))
    for pid in (int(ready.name.removeprefix("ready-")), int(ready.read_text())):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert not (tmp_path / "request.owned").exists()
    assert not (tmp_path / "out").exists()


@requires_posix
@pytest.mark.race
def test_overlapping_builtin_and_plugin_cancel_independently(
    allocated_provider, tmp_path, monkeypatch
):
    agent, invoker, receipts = allocated_provider
    generic = agent.model_copy(update={"provider_kind": "generic"})
    monkeypatch.setenv("CREWPLANE_FAKE_SCENARIO", "hold-run")

    async def scenario():
        plugin_task = asyncio.create_task(
            invoker.invoke(agent, None, "plugin", tmp_path / "plugin.out", tmp_path)
        )
        builtin_task = None
        try:
            await wait_for_receipt(receipts, "ready-*")
            builtin_task = asyncio.create_task(
                invoker.invoke(
                    generic, None, "builtin", tmp_path / "builtin.out", tmp_path
                )
            )
            async with asyncio.timeout(10):
                while len(list(receipts.glob("ready-*"))) != 2:
                    await asyncio.sleep(0.01)
            plugin_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await plugin_task
            assert not builtin_task.done()
            assert not (tmp_path / "plugin.owned").exists()
            (receipts / "release").touch()
            await asyncio.wait_for(builtin_task, 10)
            assert (tmp_path / "builtin.out").read_text().strip() == "answer: builtin"
            assert not (tmp_path / "plugin.out").exists()
        finally:
            tasks = (
                [plugin_task] if builtin_task is None else [plugin_task, builtin_task]
            )
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())
