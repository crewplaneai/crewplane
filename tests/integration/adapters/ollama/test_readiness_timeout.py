import asyncio
from dataclasses import replace

import pytest

from crewplane.architecture.contracts import CommandResult
from tests.integration.adapters.ollama.test_availability import request_for


@pytest.mark.parametrize("configured", [None, 0.02])
def test_readiness_uses_configured_deadline_or_bounded_default(
    ollama, tmp_path, monkeypatch, configured
):
    cleaned = []

    async def command(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(kwargs["cmd"][1])

    monkeypatch.setattr(ollama.availability, "run_command_once", command)
    monkeypatch.setattr(
        ollama.availability, "PROBE_TIMEOUT_SECONDS", 0.02 if configured is None else 60
    )
    request = request_for(ollama, tmp_path)
    request = replace(
        request,
        config=request.config.model_copy(
            update={"invocation_timeout_seconds": configured}
        ),
    )

    async def run():
        with pytest.raises(ValueError, match=r"deadline exceeded \(0.02s\)"):
            await asyncio.wait_for(ollama.capability.check_availability(request), 1)

    asyncio.run(run())
    assert cleaned == ["show"]


def test_larger_readiness_budget_allows_slow_model(ollama, tmp_path, monkeypatch):
    monkeypatch.setattr(ollama.availability, "PROBE_TIMEOUT_SECONDS", 0.01)
    request = request_for(ollama, tmp_path)
    request = replace(
        request,
        config=request.config.model_copy(update={"invocation_timeout_seconds": 1.0}),
    )

    async def run():
        ready = asyncio.Event()

        async def command(**kwargs):
            if kwargs["cmd"][1] == "run":
                timer = asyncio.get_running_loop().call_later(0.05, ready.set)
                try:
                    await ready.wait()
                finally:
                    timer.cancel()
            return CommandResult(0, "OK", "")

        monkeypatch.setattr(ollama.availability, "run_command_once", command)
        await asyncio.wait_for(ollama.capability.check_availability(request), 2)

    asyncio.run(run())
