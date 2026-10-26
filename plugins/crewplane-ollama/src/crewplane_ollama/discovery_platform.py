from __future__ import annotations

import ipaddress
import os
import platform
import sysconfig
from pathlib import Path
from urllib.parse import urlsplit

from crewplane.adapters.invokers.cli_invoker import CliDiscoveryRequest


def running_in_wsl(request: CliDiscoveryRequest) -> bool:
    return platform.system() == "Linux" and (
        "microsoft" in platform.release().lower()
        or bool(request.environment.get("WSL_DISTRO_NAME"))
        or bool(request.environment.get("WSL_INTEROP"))
    )


def discover_client(request: CliDiscoveryRequest) -> tuple[str, ...]:
    windows = platform.system() == "Windows"
    name = "ollama-agent.exe" if windows else "ollama-agent"
    executable = request.executable_lookup(name)
    if executable is None:
        executable = str(Path(sysconfig.get_path("scripts")) / name)
    path = (request.project_root / executable).resolve()
    if not path.is_file() or (not windows and not os.access(path, os.X_OK)):
        return ()
    if running_in_wsl(request):
        with path.open("rb") as stream:
            if path.suffix.lower() == ".exe" or stream.read(2) == b"MZ":
                return ()
    return (str(path),)


def default_route_host() -> str | None:
    """Read the WSL NAT gateway; DNS tunneling makes resolv.conf unsuitable."""
    try:
        routes = Path("/proc/net/route").read_text(encoding="ascii")
    except OSError:
        return None
    return parse_default_route(routes)


def parse_default_route(routes: str) -> str | None:
    candidates: list[tuple[int, str]] = []
    for line in routes.splitlines()[1:]:
        fields = line.split()
        # A VPN's 0.0.0.0/1 route also has a zero destination.
        # Require a zero netmask to select only default routes.
        if len(fields) < 8 or fields[1] != "00000000" or fields[7] != "00000000":
            continue
        try:
            if int(fields[3], 16) & 3 != 3:
                continue
            host = str(ipaddress.IPv4Address(bytes.fromhex(fields[2])[::-1]))
            if host != "0.0.0.0":
                candidates.append((int(fields[6]), host))
        except ValueError:
            continue
    return min(candidates)[1] if candidates else None


def endpoint_candidates(request: CliDiscoveryRequest) -> tuple[str, ...]:
    if "OLLAMA_HOST" in request.environment:
        return (normalize_endpoint(request.environment["OLLAMA_HOST"]),)
    endpoints = ["http://127.0.0.1:11434"]
    if running_in_wsl(request) and (host := default_route_host()):
        endpoints.append(f"http://{host}:11434")
    return tuple(dict.fromkeys(endpoints))


def normalize_endpoint(value: str) -> str:
    explicit_scheme = "://" in value
    parsed = urlsplit(value.strip() if explicit_scheme else "http://" + value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or any(character.isspace() for character in parsed.netloc)
    ):
        raise ValueError(
            "OLLAMA_HOST must be an HTTP(S) host with an optional port, without credentials or a path."
        )
    host = parsed.hostname
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    if parsed.port == 0:
        raise ValueError("OLLAMA_HOST port must be between 1 and 65535.")
    port = parsed.port or (
        (443 if parsed.scheme == "https" else 80) if explicit_scheme else 11434
    )
    if ":" in host:
        host = f"[{host}]"
    return f"{parsed.scheme}://{host}:{port}"
