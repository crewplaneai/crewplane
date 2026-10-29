"""Observe collection, function fixtures, and child inheritance with scalar receipts."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path.cwd()
WORKER = os.environ.get("PYTEST_XDIST_WORKER", "controller")
COLLECTION_HOME = os.environ["HOME"]
COLLECTION_CACHE = os.environ["XDG_CACHE_HOME"]
EXPECTED = json.loads((ROOT / "expected.json").read_text(encoding="utf-8"))
host = os.environ.get("OLLAMA_HOST")
assert host == EXPECTED["host"]
for key in ("OPENAI_API_KEY", "PYTHONPATH", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
    present = key in os.environ
    assert not present, key
assert importlib.util.find_spec("ambient_only_module") is None
(Path(COLLECTION_CACHE) / "collection-write").write_text("owned", encoding="utf-8")


@pytest.mark.parametrize("case", range(4))
def test_environment(case):
    home, cache = os.environ["HOME"], os.environ["XDG_CACHE_HOME"]
    assert home != COLLECTION_HOME
    assert cache != COLLECTION_CACHE
    marker = Path(cache) / "test-write"
    assert not marker.exists()
    marker.write_text("owned", encoding="utf-8")
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, os; print(json.dumps({key: os.environ.get(key) for key in ('HOME', 'XDG_CACHE_HOME', 'OLLAMA_HOST', 'OPENAI_API_KEY')}))",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    inherited = json.loads(child.stdout)
    child_home = inherited["HOME"]
    child_cache = inherited["XDG_CACHE_HOME"]
    child_host = inherited["OLLAMA_HOST"]
    child_has_api_key = inherited["OPENAI_API_KEY"] is not None
    assert child_home == home
    assert child_cache == cache
    assert child_host == EXPECTED["host"]
    assert not child_has_api_key
    receipt = {
        "case": case,
        "worker": WORKER,
        "home": home,
        "cache": cache,
        "collection_home": COLLECTION_HOME,
        "collection_cache": COLLECTION_CACHE,
        "child_host": child_host,
    }
    (ROOT / f"receipt-{case}.json").write_text(json.dumps(receipt), encoding="utf-8")
    if EXPECTED["fail"] and case == 0:
        raise AssertionError("original scenario failure")
