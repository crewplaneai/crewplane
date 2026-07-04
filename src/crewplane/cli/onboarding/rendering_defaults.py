from __future__ import annotations

from ..templates import (
    CONFIG_TEMPLATE,
    DEFAULT_WORKFLOW_TEMPLATE,
    render_template_content,
)


def rendered_default_config() -> str:
    return render_template_content(CONFIG_TEMPLATE.read_text(encoding="utf-8"))


def rendered_default_workflow() -> str:
    return render_template_content(
        DEFAULT_WORKFLOW_TEMPLATE.read_text(encoding="utf-8")
    )


__all__ = [
    "rendered_default_config",
    "rendered_default_workflow",
]
