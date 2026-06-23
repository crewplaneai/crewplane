from __future__ import annotations

from pathlib import Path

from .source import (
    PreflightWorkflowSource,
)
from .source import (
    load_workflow_source_for_preflight as _load_workflow_source_for_preflight,
)


def load_workflow_source_for_preflight(
    tasks_file: Path,
    project_root: Path,
) -> PreflightWorkflowSource:
    return _load_workflow_source_for_preflight(tasks_file, project_root)
