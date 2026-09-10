from __future__ import annotations

from pathlib import Path

from tests.helpers.workspace_service import (
    run_git_text,
)


def workspace_run_refs(repo: Path) -> str:
    return run_git_text(
        repo,
        "for-each-ref",
        "--format=%(refname)",
        "refs/crewplane/runs/workspace-run-001",
    )
