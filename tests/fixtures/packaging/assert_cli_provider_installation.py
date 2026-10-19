"""Run with the isolated environment interpreter, outside checkout imports."""

import asyncio
import copy
import importlib.metadata
import json
import sys
from functools import partial
from pathlib import Path

import crewplane_fixture_provider
import crewplane_ollama
from rich.console import Console

import crewplane
from crewplane.adapters.invokers.cli import CliInvokerAdapter
from crewplane.adapters.invokers.cli_invoker import CliDiscoveryRequest
from crewplane.adapters.invokers.cli_invoker.capabilities import CAPABILITIES
from crewplane.bootstrap import build_runtime_config_snapshot
from crewplane.core.config import Config
from crewplane.version import SCHEMA_VERSION


def rejects(action, match):
    try:
        action()
    except ValueError as exc:
        assert match in str(exc), str(exc)
    else:
        raise AssertionError(f"Expected rejection: {match}")


async def check_installed_discovery():
    onboarding = crewplane_ollama.OLLAMA.onboarding
    assert onboarding is not None
    requests = []

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            requests.append(header.split(b" ")[1])
            writer.write(
                b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with asyncio.timeout(30), asyncio.TaskGroup() as connections:

        def accept(reader, writer):
            connections.create_task(respond(reader, writer))

        server = await asyncio.start_server(accept, "127.0.0.1", 0)
        async with server:
            endpoint = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            request = CliDiscoveryRequest(Path.cwd(), {"OLLAMA_HOST": endpoint}, {}.get)
            result = await onboarding.discover(request)
            assert result.endpoints == ()
            assert any("HTTPStatusError" in item for item in result.diagnostics), (
                result.diagnostics
            )
    assert requests == [b"/api/version"]


def main():
    for module in (crewplane, crewplane_fixture_provider, crewplane_ollama):
        assert Path(module.__file__).is_relative_to(sys.prefix), module.__file__
    registration = {
        "implementation": "crewplane_fixture_provider:FIXTURE",
        "distribution": "crewplane-fixture-provider",
    }
    config = Config(
        version=SCHEMA_VERSION,
        agents={
            "local": {
                "cli_cmd": [
                    sys.executable,
                    "-c",
                    "import sys; print(sys.stdin.read())",
                ],
                "provider_kind": "fixture",
            }
        },
        settings={
            "integrations": {
                "invoker": {
                    "implementation": "cli",
                    "options": {"providers": {"fixture": registration}},
                }
            }
        },
    )
    snapshot = build_runtime_config_snapshot(config, Console(), True)
    adapter = CliInvokerAdapter()
    canonical = snapshot.invoker_options
    invoker = adapter.create_invoker(config, canonical)
    assert "fixture" not in CAPABILITIES
    output = Path.cwd() / "answer.txt"
    asyncio.run(
        invoker.invoke(
            config.agents["local"], None, "installed café", output, Path.cwd()
        )
    )
    assert output.read_text().strip() == "installed café"
    for changes, match in [
        ({"extra": "bad"}, "exactly"),
        ({"distribution": "missing-fixture-distribution"}, "not installed"),
        (
            {"implementation": "crewplane_fixture_provider:INVALID_EXPORT"},
            "CliProviderCapability",
        ),
        (
            {"implementation": "crewplane_fixture_provider:INVALID_CALLBACK"},
            "build_command",
        ),
        (
            {"implementation": "crewplane_fixture_provider:WRONG_IDENTITY"},
            "identity mismatch",
        ),
    ]:
        options = {"providers": {"fixture": {**registration, **changes}}}
        rejects(partial(adapter.canonicalize_options, "cli", "cli", options), match)
    rejects(
        lambda: adapter.canonicalize_options(
            "cli", "cli", {"providers": {"generic": registration}}
        ),
        "reserved",
    )
    drift = copy.deepcopy(canonical)
    drift["providers"]["fixture"]["version"] = "different"
    rejects(lambda: adapter.create_invoker(config, drift), "version mismatch")
    drift["providers"]["fixture"]["version"] = "1.2.3"
    drift["providers"]["fixture"]["implementation"] = (
        "crewplane_fixture_provider:WRONG_IDENTITY"
    )
    rejects(lambda: adapter.create_invoker(config, drift), "identity mismatch")
    distribution = importlib.metadata.distribution("crewplane-fixture-provider")
    metadata = next(
        distribution.locate_file(item)
        for item in distribution.files
        if str(item).endswith(".dist-info/METADATA")
    )
    original = metadata.read_bytes()
    try:
        metadata.write_text("Name: another-identity\nVersion: 1.2.3\n")
        rejects(lambda: adapter.create_invoker(config, canonical), "identity mismatch")
    finally:
        metadata.write_bytes(original)
    actual = importlib.metadata.distribution("crewplane-ollama")
    assert "crewplane>=0.3.8" in actual.requires
    assert any(str(item) == "crewplane_ollama/py.typed" for item in actual.files)
    assert crewplane_ollama.OLLAMA.provider_kind == "ollama"
    asyncio.run(check_installed_discovery())
    actual_options = adapter.canonicalize_options(
        "cli",
        "cli",
        {
            "providers": {
                "ollama": {
                    "implementation": "crewplane_ollama:OLLAMA",
                    "distribution": "crewplane-ollama",
                }
            }
        },
    ).options
    assert actual_options["providers"]["ollama"]["version"] == actual.version
    print(
        json.dumps(
            {"fixture": canonical, "ollama": actual_options, "prefix": sys.prefix}
        )
    )


if __name__ == "__main__":
    main()
