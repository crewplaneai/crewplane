import importlib
from types import SimpleNamespace

import pytest

from tests.helpers.cli_provider import OLLAMA_PACKAGE, expose_distribution, fake_ollama


@pytest.fixture
def ollama(tmp_path, monkeypatch):
    expose_distribution(
        monkeypatch, tmp_path, OLLAMA_PACKAGE, "crewplane-ollama", "0.1.0"
    )
    executable, receipts = fake_ollama(monkeypatch, tmp_path)
    return SimpleNamespace(
        capability=importlib.import_module("crewplane_ollama").OLLAMA,
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
