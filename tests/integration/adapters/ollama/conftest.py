import importlib
import os
from types import SimpleNamespace

import httpx
import pytest

from tests.helpers.cli_provider import (
    OLLAMA_PACKAGE,
    expose_distribution,
    fake_ollama_agent,
)


@pytest.fixture
def ollama_scripts(tmp_path):
    scripts = tmp_path / "installed scripts"
    scripts.mkdir()
    return scripts


@pytest.fixture
def ollama_package(tmp_path, monkeypatch, ollama_scripts):
    expose_distribution(
        monkeypatch, tmp_path, OLLAMA_PACKAGE, "crewplane-ollama", "0.2.0"
    )
    monkeypatch.setenv("PYTHONPATH", str(OLLAMA_PACKAGE / "src"), prepend=os.pathsep)
    package = importlib.import_module("crewplane_ollama")
    platform = importlib.import_module("crewplane_ollama.discovery_platform")
    monkeypatch.setattr(
        platform,
        "sysconfig",
        SimpleNamespace(get_path={"scripts": str(ollama_scripts)}.__getitem__),
    )
    return package


@pytest.fixture
def ollama(ollama_package, tmp_path, monkeypatch):
    executable, receipts = fake_ollama_agent(monkeypatch, tmp_path)
    return SimpleNamespace(
        capability=ollama_package.OLLAMA,
        availability=importlib.import_module("crewplane_ollama.availability"),
        executable=executable,
        receipts=receipts,
        options={
            "providers": {
                "ollama": {
                    "implementation": "crewplane_ollama:OLLAMA",
                    "distribution": "crewplane-ollama",
                }
            }
        },
    )


@pytest.fixture
def discovery_server(ollama_package, monkeypatch):
    del ollama_package
    discovery = importlib.import_module("crewplane_ollama.discovery")
    worker = importlib.import_module("crewplane_ollama.discovery_worker")

    async def query(task, project_root):
        return await worker.query_server(task, project_root / "discovery-report.json")

    def install(handler):
        def client(timeout_seconds):
            return httpx.AsyncClient(
                timeout=timeout_seconds, transport=httpx.MockTransport(handler)
            )

        monkeypatch.setattr(worker, "discovery_client", client)

    monkeypatch.setattr(discovery, "run_discovery", query)
    return install
