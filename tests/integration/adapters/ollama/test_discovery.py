import asyncio
import importlib
import shutil

import httpx
import pytest

from crewplane.adapters.invokers.cli_invoker import CliDiscoveryRequest
from tests.helpers.platforms import requires_posix


@pytest.fixture
def discovery(ollama_package, monkeypatch):
    del ollama_package
    module = importlib.import_module("crewplane_ollama.discovery")
    platform = importlib.import_module("crewplane_ollama.discovery_platform")
    monkeypatch.setattr(platform.platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform.platform, "release", lambda: "linux")
    monkeypatch.setattr(platform, "default_route_host", lambda: "172.20.0.1")
    return module, platform


@pytest.mark.parametrize(
    "system,wsl",
    [("Linux", False), ("Darwin", False), ("Windows", False), ("Linux", True)],
)
def test_discovery_finds_installed_agent_without_path(
    discovery, ollama_scripts, tmp_path, monkeypatch, system, wsl
):
    _, platform = discovery
    monkeypatch.setattr(platform.platform, "system", lambda: system)
    name = (
        "crewplane-ollama-agent.exe"
        if system == "Windows"
        else "crewplane-ollama-agent"
    )
    executable = ollama_scripts / name
    executable.write_bytes(b"MZ" if system == "Windows" else b"#!/bin/sh\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", "")
    assert shutil.which(name) is None
    request = CliDiscoveryRequest(
        tmp_path, {"WSL_DISTRO_NAME": "Ubuntu"} if wsl else {}, shutil.which
    )

    assert platform.discover_client(request) == (str(executable),)


def test_discovery_prefers_lookup_over_installed_agent(
    discovery, ollama, ollama_scripts, tmp_path
):
    _, platform = discovery
    installed = ollama_scripts / "crewplane-ollama-agent"
    installed.write_bytes(b"#!/bin/sh\n")
    installed.chmod(0o755)
    request = CliDiscoveryRequest(
        tmp_path, {}, {"crewplane-ollama-agent": str(ollama.executable)}.get
    )

    assert platform.discover_client(request) == (str(ollama.executable),)


@requires_posix
@pytest.mark.parametrize("kind", ["directory", "not-executable", "windows-binary"])
def test_discovery_rejects_unusable_installed_agent(
    discovery, ollama_scripts, tmp_path, kind
):
    _, platform = discovery
    executable = ollama_scripts / "crewplane-ollama-agent"
    if kind == "directory":
        executable.mkdir()
    else:
        executable.write_bytes(b"MZ" if kind == "windows-binary" else b"#!/bin/sh\n")
        executable.chmod(0o644 if kind == "not-executable" else 0o755)
    request = CliDiscoveryRequest(tmp_path, {"WSL_DISTRO_NAME": "Ubuntu"}, {}.get)

    assert platform.discover_client(request) == ()


@pytest.mark.parametrize(
    "system,wsl,expected",
    [
        ("Linux", False, ["127.0.0.1"]),
        ("Darwin", False, ["127.0.0.1"]),
        ("Windows", False, ["127.0.0.1"]),
        ("Linux", True, ["127.0.0.1", "172.20.0.1"]),
    ],
)
def test_discovery_uses_platform_endpoints(
    ollama, discovery, discovery_server, tmp_path, monkeypatch, system, wsl, expected
):
    module, platform = discovery
    monkeypatch.setattr(platform.platform, "system", lambda: system)
    calls = []

    def respond(request):
        calls.append(request.url.host)
        return httpx.Response(200, json={"version": "0.12.0"})

    discovery_server(respond)
    request = CliDiscoveryRequest(
        tmp_path,
        {"WSL_DISTRO_NAME": "Ubuntu"} if wsl else {},
        {
            "crewplane-ollama-agent": str(ollama.executable),
            "crewplane-ollama-agent.exe": str(ollama.executable),
        }.get,
    )
    result = asyncio.run(module.discover(request))
    assert result.command == (str(ollama.executable),)
    assert calls == expected
    assert [endpoint.environment["OLLAMA_HOST"] for endpoint in result.endpoints] == [
        f"http://{host}:11434" for host in expected
    ]
    if system == "Windows":
        assert "acceptance is still pending" in " ".join(result.diagnostics)


def test_windows_discovery_requires_installed_harness_not_original_client(
    discovery, tmp_path, monkeypatch
):
    _, platform = discovery
    monkeypatch.setattr(platform.platform, "system", lambda: "Windows")
    local = tmp_path / "Windows User" / "AppData" / "Local"
    executable = local / "Programs/Ollama/ollama.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"MZ")
    request = CliDiscoveryRequest(tmp_path, {"LOCALAPPDATA": str(local)}, {}.get)
    assert platform.discover_client(request) == ()


@pytest.mark.parametrize(
    "client", [None, "crewplane-ollama-agent.exe", "renamed-windows-client"]
)
def test_wsl_reachable_windows_server_requires_native_harness(
    discovery, discovery_server, tmp_path, client
):
    module, _ = discovery
    executable = tmp_path / (client or "windows-ollama.exe")
    executable.write_bytes(b"MZ")
    executable.chmod(0o755)

    def respond(request):
        if request.url.host == "127.0.0.1":
            raise httpx.ConnectError("closed")
        return httpx.Response(200, json={"version": "1"})

    discovery_server(respond)
    found = {"crewplane-ollama-agent.exe": str(executable)}
    if client:
        found["crewplane-ollama-agent"] = str(executable)
    result = asyncio.run(
        module.discover(
            CliDiscoveryRequest(
                tmp_path, {"WSL_INTEROP": "/run/WSL/1_interop"}, found.get
            )
        )
    )
    assert result.command == ()
    assert len(result.endpoints) == 1
    assert result.endpoints[0].label == "http://172.20.0.1:11434"
    assert "crewplane-ollama-agent not found" in " ".join(result.diagnostics)


@pytest.mark.parametrize("response", [{"version": "1"}, {"unrelated": True}])
def test_explicit_endpoint_is_authoritative(
    ollama, discovery, discovery_server, tmp_path, response
):
    module, _ = discovery
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=response)

    discovery_server(respond)
    result = asyncio.run(
        module.discover(
            CliDiscoveryRequest(
                tmp_path,
                {"OLLAMA_HOST": "my-server:14444", "WSL_DISTRO_NAME": "Ubuntu"},
                {"crewplane-ollama-agent": str(ollama.executable)}.get,
            )
        )
    )
    assert calls == ["http://my-server:14444/api/version"]
    assert len(result.endpoints) == (1 if "version" in response else 0)
    if not result.endpoints:
        assert "no other endpoint was tried" in " ".join(result.diagnostics)


def test_discovery_filters_cloud_and_embedding_models(
    ollama, discovery, discovery_server, tmp_path
):
    module, _ = discovery
    calls = []

    def respond(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "1"})
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": name}
                        for name in ("text", "embed", "cloud", "broken", "no-tools")
                    ]
                },
            )
        import json

        name = json.loads(request.content)["model"]
        if name == "broken":
            return httpx.Response(404)
        details = {
            "capabilities": ["embedding"]
            if name == "embed"
            else ["completion", "tools"]
        }
        if name == "no-tools":
            details["capabilities"] = ["completion"]
        if name == "cloud":
            details["remote_host"] = "https://cloud.invalid"
        return httpx.Response(200, json=details)

    discovery_server(respond)
    request = CliDiscoveryRequest(
        tmp_path, {}, {"crewplane-ollama-agent": str(ollama.executable)}.get
    )

    async def inspect():
        result = await module.discover(request)
        return await module.list_models(request, result.endpoints[0])

    models = asyncio.run(inspect())
    assert models.models == ("text",)
    assert any("Skipped broken" in item for item in models.diagnostics)
    assert all(path in {"/api/version", "/api/tags", "/api/show"} for _, path in calls)


@pytest.mark.parametrize(
    "payload", [{"models": []}, {}, {"models": [{"name": "-bad"}]}]
)
def test_unusable_model_list_has_actionable_diagnostic(
    discovery, discovery_server, tmp_path, payload
):
    module, _ = discovery

    def handler(request):
        del request
        return httpx.Response(200, json=payload)

    discovery_server(handler)
    from crewplane.adapters.invokers.cli_invoker import CliDiscoveredEndpoint

    result = asyncio.run(
        module.list_models(
            CliDiscoveryRequest(tmp_path, {}, {}.get),
            CliDiscoveredEndpoint("local", {"OLLAMA_HOST": "http://localhost:11434"}),
        )
    )
    assert result.models == ()
    diagnostic = " ".join(result.diagnostics)
    if payload == {"models": []}:
        assert "ollama pull <model>" in diagnostic
    else:
        assert "discovery did not complete" in diagnostic
        assert "No installed" not in diagnostic


@pytest.mark.parametrize(
    "endpoint,expected",
    [
        ("localhost", "http://localhost:11434"),
        ("https://localhost", "https://localhost:443"),
        ("0.0.0.0:11434", "http://127.0.0.1:11434"),
        ("[::1]:11434", "http://[::1]:11434"),
        ("[::]:11434", "http://[::1]:11434"),
    ],
)
def test_endpoint_normalization(discovery, endpoint, expected):
    assert discovery[1].normalize_endpoint(endpoint) == expected


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        "http://user:password@host",
        "ftp://host",
        "http://host/path",
        "host:bad",
        "localhost:0",
    ],
)
def test_invalid_explicit_endpoint_is_not_replaced(discovery, tmp_path, endpoint):
    with pytest.raises(ValueError):
        discovery[1].endpoint_candidates(
            CliDiscoveryRequest(tmp_path, {"OLLAMA_HOST": endpoint}, {}.get)
        )


def test_gateway_uses_default_route_metric_and_little_endian_address(discovery):
    routes = "Iface Destination Gateway Flags RefCnt Use Metric Mask\neth0 00000000 010014AC 0003 0 0 100 00000000\neth1 00000000 010015AC 0003 0 0 10 00000000\neth2 00000000 garbage 0003 0 0 1 00000000\ntun0 00000000 0100080A 0003 0 0 0 00000080\n"
    assert discovery[1].parse_default_route(routes) == "172.21.0.1"
    assert discovery[1].parse_default_route("Iface Destination Gateway\n") is None


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("-inf"), float("nan")])
def test_discovery_request_rejects_invalid_timeout(tmp_path, timeout):
    with pytest.raises(ValueError, match="finite number greater than 0"):
        CliDiscoveryRequest(tmp_path, {}, {}.get, timeout)
