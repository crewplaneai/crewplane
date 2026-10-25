import asyncio
from dataclasses import replace

import pytest

from crewplane.architecture.contracts import CommandResult
from tests.integration.adapters.ollama.test_availability import request_for


def test_readiness_uses_configured_finite_deadline(ollama, tmp_path, monkeypatch):
    cleaned = []

    async def command(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(kwargs["cmd"][1])

    monkeypatch.setattr(ollama.availability, "run_command_once", command)
    request = request_for(ollama, tmp_path, 0.02)

    async def run():
        with pytest.raises(ValueError, match=r"deadline exceeded \(0.02s\)"):
            await asyncio.wait_for(ollama.capability.check_availability(request), 1)

    asyncio.run(run())
    assert cleaned == ["--check"]


def test_readiness_rejects_null_deadline_without_launch(ollama, tmp_path, monkeypatch):
    def forbidden(**kwargs):
        del kwargs
        pytest.fail("invalid profiles must not launch a probe")

    monkeypatch.setattr(ollama.availability, "run_command_once", forbidden)
    request = request_for(ollama, tmp_path)
    request = replace(
        request,
        config=request.config.model_copy(update={"invocation_timeout_seconds": None}),
    )
    with pytest.raises(ValueError, match="finite positive"):
        asyncio.run(ollama.capability.check_availability(request))


def test_larger_readiness_budget_allows_slow_model(ollama, tmp_path, monkeypatch):
    request = request_for(ollama, tmp_path, 1.0)

    async def run():
        ready = asyncio.Event()

        async def command(**kwargs):
            assert kwargs["cmd"][1] == "--check"
            timer = asyncio.get_running_loop().call_later(0.05, ready.set)
            try:
                await ready.wait()
            finally:
                timer.cancel()
            return CommandResult(0, "OK", "")

        monkeypatch.setattr(ollama.availability, "run_command_once", command)
        await asyncio.wait_for(ollama.capability.check_availability(request), 2)

    asyncio.run(run())
