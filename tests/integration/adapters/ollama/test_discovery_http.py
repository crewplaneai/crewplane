import asyncio
import importlib

import httpx
import pytest

pytestmark = pytest.mark.usefixtures("ollama_package")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": "http://elsewhere.invalid"}),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1)),
    ],
)
def test_discovery_rejects_redirects_invalid_and_oversized_responses(response):
    module = importlib.import_module("crewplane_ollama.discovery_http")

    def handler(request):
        del request
        return response

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError):
                await module.read_json(client, "http://localhost:11434/api/version")

    asyncio.run(run())


@pytest.mark.race
def test_discovery_cancellation_closes_stream():
    module = importlib.import_module("crewplane_ollama.discovery_http")

    async def run():
        started = asyncio.Event()

        class Stream(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                started.set()
                await asyncio.Future()
                yield b""

            async def aclose(self):
                self.closed = True

        stream = Stream()

        def handler(request):
            del request
            return httpx.Response(200, stream=stream)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            task = asyncio.create_task(
                module.read_json(client, "http://localhost:11434/api/version")
            )
            try:
                await asyncio.wait_for(started.wait(), timeout=2)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert stream.closed

    asyncio.run(run())
