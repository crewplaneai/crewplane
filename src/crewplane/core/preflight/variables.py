from __future__ import annotations

from pathlib import Path


def build_builtin_template_variables(project_root: Path) -> dict[str, str]:
    """Build deterministic template variables derived from the project root."""

    return {"project_name": project_root.resolve().name}
