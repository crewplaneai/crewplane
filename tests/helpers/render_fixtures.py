from __future__ import annotations

from pathlib import Path


def read_render_fixture(root: Path, case_id: str, artifact_name: str) -> str:
    return (root / case_id / artifact_name).read_text(encoding="utf-8").rstrip("\n")
