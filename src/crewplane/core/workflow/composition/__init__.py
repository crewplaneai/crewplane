from __future__ import annotations

from pathlib import Path

from .models import ComposedWorkflowDocument
from .traversal import WorkflowComposer


def compose_workflow_markdown(
    path: Path,
    project_root: Path | None = None,
) -> ComposedWorkflowDocument:
    composer = WorkflowComposer(project_root=project_root or Path.cwd())
    return composer.compose(path)
