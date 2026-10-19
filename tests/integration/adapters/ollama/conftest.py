import importlib
import os
from types import SimpleNamespace

import httpx
import pytest

from tests.helpers.cli_provider import OLLAMA_PACKAGE, expose_distribution, fake_ollama


@pytest.fixture
def ollama_package(tmp_path, monkeypatch):
    expose_distribution(
        monkeypatch, tmp_path, OLLAMA_PACKAGE, "crewplane-ollama", "0.1.0"
    )
    monkeypatch.setenv("PYTHONPATH", str(OLLAMA_PACKAGE / "src"), prepend=os.pathsep)
    return importlib.import_module("crewplane_ollama")


@pytest.fixture
def ollama(ollama_package, tmp_path, monkeypatch):
    executable, receipts = fake_ollama(monkeypatch, tmp_path)
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
