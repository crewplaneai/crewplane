"""Observe launcher isolation before pytest configures worker-specific locations."""

import os
from pathlib import Path

pytest_plugins = ("tests.helpers.pytest_environment",)

cache = Path(os.environ["XDG_CACHE_HOME"])
(cache / "startup-write").write_text("owned", encoding="utf-8")
execution_root = Path(os.environ["CREWPLANE_TEST_ENV_ROOT"])
assert cache.is_relative_to(execution_root), "startup used an external cache"
for key in ("OPENAI_API_KEY", "PYTHONPATH", "PYTEST_ADDOPTS"):
    present = key in os.environ
    assert not present, key
