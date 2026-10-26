import asyncio

import pytest

from crewplane.architecture.contracts import CommandResult
from tests.integration.adapters.ollama.test_availability import request_for


@pytest.mark.parametrize("invocation_timeout", [None, 60])
def test_readiness_deadline_is_independent_of_invocation_timeout(
    ollama, tmp_path, monkeypatch, invocation_timeout
):
    cleaned = []

    async def command(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(kwargs["cmd"][1])

    monkeypatch.setattr(ollama.availability, "run_command_once", command)
    request = request_for(ollama, tmp_path, 0.02)
    request.config.invocation_timeout_seconds = invocation_timeout

    async def run():
        with pytest.raises(
            ValueError, match=r"deadline exceeded \(readiness_timeout_seconds=0.02s\)"
        ):
            await asyncio.wait_for(ollama.capability.check_availability(request), 1)

    asyncio.run(run())
    assert cleaned == ["--check"]


def test_readiness_accepts_unlimited_execution(ollama, tmp_path, monkeypatch):
    commands = []

    async def command(**kwargs):
        commands.append(kwargs["cmd"])
        return CommandResult(0, "OK", "")

    monkeypatch.setattr(ollama.availability, "run_command_once", command)
    request = request_for(ollama, tmp_path)
    asyncio.run(ollama.capability.check_availability(request))
    assert commands == [[str(ollama.executable), "--check", "--model", request.model]]


def test_readiness_can_outlast_invocation_timeout(ollama, tmp_path, monkeypatch):
    request = request_for(ollama, tmp_path, 1.0)
    request.config.invocation_timeout_seconds = 0.001

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
