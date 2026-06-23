from __future__ import annotations

import os
import platform
from pathlib import Path


def workspace_cache_root(cache_root: str | None) -> Path:
    if cache_root:
        return Path(cache_root).expanduser()
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Caches" / "crewplane"
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "crewplane"
